import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, db
from .examples import register_legend
from .quiz import Question, build_daily_set, build_one, build_question_from_story, build_questions_from_rows

logger = logging.getLogger(__name__)

LETTERS = ["A", "B", "C", "D"]

# Cache message_id → (stem, kind, options) so handle_answer can show the filled-in sentence.
# Lost on restart (acceptable — fallback to meaning-only display).
_stem_cache: dict[int, tuple[str, str, list]] = {}

# Production-question pending state is now persisted in DB (db.production_cache).
# See db.save_production_pending / db.get_production_pending.

PRODUCTION_EVAL_PROMPT = """You are an English teacher helping a Vietnamese learner practice using the English idiom "{phrase}".

Target idiom: "{phrase}"
Meaning: {meaning}

Their sentence: "{sentence}"

The goal is that they LEARN THIS IDIOM. Every INCORRECT reply must show them how the idiom is actually used.

Respond in EXACTLY this format — keep it tight:
Line 1: CORRECT or INCORRECT
Line 2: ONE short sentence of feedback. If INCORRECT, briefly say what went wrong (wrong meaning, wrong idiom, wrong grammar, etc.).
Line 3: If INCORRECT, "Example: <one natural sentence that uses the target idiom \"{phrase}\" correctly>". If CORRECT, skip this line.
Line 4: "Similar: <1-3 close idioms>" — list 1 to 3 DIFFERENT English idioms with similar meaning, comma-separated. Do NOT list variants of the target idiom itself. Skip this line only if no good similar idioms exist.

Rules:
- The Example line MUST include the target idiom "{phrase}" verbatim (or its natural inflected form, e.g. tense change).
- Be honest but not preachy. No encouragement filler. No apologies.
- Do NOT exceed 4 lines total."""


# Fields a content fix may rewrite. Anything outside this set is ignored, so a
# stray line in the model's reply cannot reach the UPDATE statement.
FIXABLE_FIELDS = {
    "phrase": "the idiom headword itself",
    "meaning": "the English definition",
    "vietnamese_equiv": "the Vietnamese equivalent",
    "example": "the example sentence",
    "story": "the mini-story",
}

CONTENT_FIX_PROMPT = """You maintain an English-idiom study database for a Vietnamese learner.
They have reported something while looking at a quiz question. Work out what they
mean, then decide whether the stored entry is actually wrong.

The quiz question they were looking at:
---
{question}
---

The stored entry:
phrase: {phrase}
meaning: {meaning}
vietnamese_equiv: {vietnamese_equiv}
example: {example}
story: {story}

Their report: "{complaint}"

First decide what the report is about. Only a problem with one of the five stored
fields above is something you can fix. These are NOT fixable here, and for any of
them you must change nothing:
- the generated question itself — a missing, confusing, or repeated situation,
  the wrong question type, the wording of the prompt
- the grading of their answer
- a request to change many entries at once, or to scan the whole database
- anything about the bot's behaviour rather than this entry's content

Output format. For each stored field that is genuinely wrong:

FIELD: <field name>
VALUE: <the corrected value, on one line>

Field names must be exactly one of: phrase, meaning, vietnamese_equiv, example, story

Then one final line:
NOTE: <one sentence to the learner>

If nothing should change — the report is about something unfixable above, the entry
is already correct, or you cannot tell what they mean — output only:
FIELD: none
NOTE: <one sentence saying plainly what you understood and why nothing changed>

Rules:
- Default to changing nothing. Only rewrite a field when the report clearly
  identifies something wrong with that field's current value.
- Never rewrite a field just to have something to show. An entry that is already
  good must be left exactly as it is.
- Change as little as possible: a report about the meaning does not license
  rewriting the story.
- example and story must contain the phrase verbatim (or a natural inflection).
- vietnamese_equiv must be idiomatic Vietnamese as a Vietnamese speaker would say
  it, not a word-for-word gloss or an explanation.
- Keep each value on a single line. No markdown, no quotes around values."""


def _parse_content_fix(raw: str) -> tuple[dict[str, str], str]:
    """Parse the model's FIELD/VALUE/NOTE reply into (updates, note).

    Only whitelisted field names survive, and a FIELD line without a following
    VALUE is dropped — a malformed reply yields no updates rather than a bad write.
    """
    updates: dict[str, str] = {}
    note = ""
    pending_field = None
    for line in raw.splitlines():
        line = line.strip()
        if line.upper().startswith("FIELD:"):
            name = line.split(":", 1)[1].strip().lower()
            pending_field = name if name in FIXABLE_FIELDS else None
        elif line.upper().startswith("VALUE:"):
            value = line.split(":", 1)[1].strip()
            if pending_field and value:
                updates[pending_field] = value
            pending_field = None
        elif line.upper().startswith("NOTE:"):
            note = line.split(":", 1)[1].strip()
    return updates, note


def _question_text(q: Question) -> str:
    opts = "\n".join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(q.options))
    prefix = "↩️ Try again — you missed this one before.\n\n" if q.reask else ""
    if q.kind == "reverse":
        return f"{prefix}What does '{q.phrase}' mean?\n\n{q.stem}\n\n{opts}"
    if q.kind == "vietnamese":
        return f"{prefix}Which English idiom matches?\n\n{q.stem}\n\n{opts}"
    if q.kind == "completion":
        return f"{prefix}{q.stem}\n\n{opts}"
    if q.kind == "production":
        return f"{prefix}✍️ Use it in a sentence!\n\n{q.stem}\n\nReply to this message with your sentence 👇"
    return f"{prefix}Fill in the blank:\n\n{q.stem}\n\n{opts}"


def _skip_button(idiom_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("⏭ I know this", callback_data=f"skip:{idiom_id}")


def _skip_only_keyboard(idiom_id: int) -> InlineKeyboardMarkup:
    """Markup left on a question once it has been answered or given up on.

    Skipping stays available afterwards: seeing the answer is often what tells
    you the idiom is already solid and does not need scheduling any more.
    """
    return InlineKeyboardMarkup([[_skip_button(idiom_id)]])


def _keyboard(q: Question) -> InlineKeyboardMarkup | None:
    controls = [
        InlineKeyboardButton("🤷 Don't know", callback_data=f"dunno:{q.idiom_id}"),
        _skip_button(q.idiom_id),
    ]
    if q.kind == "production":
        return InlineKeyboardMarkup([controls])
    buttons = [
        InlineKeyboardButton(
            LETTERS[i],
            callback_data=f"ans:{q.idiom_id}:{i}:{q.correct_index}"
        )
        for i in range(len(q.options))
    ]
    return InlineKeyboardMarkup([buttons, controls])


async def _send_question(chat_id: int, q: Question, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=_question_text(q),
        reply_markup=_keyboard(q),
    )
    # Log every sent question by (chat_id, idiom_id, date) so evening quiz
    # can exclude morning items even if the user hasn't answered them yet.
    with db.connect(config.DB_PATH) as conn:
        db.log_question_sent(conn, chat_id, q.idiom_id, config.today_local().isoformat())
        # Anchor the message to its idiom so a later reply — an answer or a
        # content complaint — can be traced back whatever the question type.
        db.log_question_msg(conn, chat_id, msg.message_id, q.idiom_id, q.kind)
        if q.kind == "production":
            db.save_production_pending(
                conn, chat_id, msg.message_id, q.idiom_id, q.phrase,
                turn_number=1,
                used_situations=(q.situation or ""),
            )
    if q.kind != "production":
        _stem_cache[msg.message_id] = (q.stem, q.kind, q.options)
        if len(_stem_cache) > 2000:
            del _stem_cache[next(iter(_stem_cache))]


DENIED_TEXT = (
    "This bot is invite-only right now, so there's nothing here for you yet. "
    "If you were expecting access, ask whoever sent you the link."
)


def _is_admin(chat_id: int) -> bool:
    return config.ADMIN_CHAT_ID != 0 and chat_id == config.ADMIN_CHAT_ID


async def _deny_if_blocked(update: Update) -> bool:
    """Tell the user access is denied and return True when it is.

    Every handler calls this first, button taps included: blocking only the
    scheduled sends would still let a stranger pull unlimited content with
    /quiz, or grade answers by tapping buttons on questions sent earlier.
    """
    chat_id = update.effective_chat.id
    with db.connect(config.DB_PATH) as conn:
        blocked = db.is_blocked(conn, chat_id)
    if not blocked:
        return False
    if update.callback_query:
        await update.callback_query.answer(DENIED_TEXT, show_alert=True)
    elif update.message:
        await update.message.reply_text(DENIED_TEXT)
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    with db.connect(config.DB_PATH) as conn:
        known = db.is_blocked(conn, chat_id) or chat_id in db.all_users(conn)
        # New arrivals start blocked while invite-only is on. A repeat /start
        # from an existing user must not change their state either way.
        db.register_user(conn, chat_id, user.username, blocked=config.INVITE_ONLY)
        blocked = db.is_blocked(conn, chat_id)

    if blocked:
        await update.message.reply_text(DENIED_TEXT)
        if not known and config.ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=config.ADMIN_CHAT_ID,
                    text=(f"🔔 Access request: {user.first_name} "
                          f"@{user.username or '—'} (`{chat_id}`)\n"
                          f"Allow with /allow {chat_id}"),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning("Couldn't notify admin of %s: %s", chat_id, e)
        return

    await update.message.reply_text(
        f"Hi {user.first_name}!\n\n"
        "I'll send you 15 idiom quizzes every day at 6 AM.\n"
        "Type /quiz anytime for an extra set, /stats to see your progress."
    )


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: list users and their access state."""
    if not _is_admin(update.effective_chat.id):
        return
    with db.connect(config.DB_PATH) as conn:
        rows = db.list_users(conn)
    lines = ["👥 *Users*", ""]
    for r in rows:
        mark = "🚫" if r["blocked"] else "✅"
        lines.append(
            f"{mark} `{r['chat_id']}` @{r['username'] or '—'} "
            f"· joined {(r['registered'] or '')[:10]}"
        )
    lines.append("")
    lines.append("/allow <id> · /block <id>")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _set_access(update: Update, blocked: bool) -> None:
    if not _is_admin(update.effective_chat.id):
        return
    args = update.message.text.split()[1:]
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /allow <chat_id> or /block <chat_id>")
        return
    target = int(args[0])
    with db.connect(config.DB_PATH) as conn:
        ok = db.set_blocked(conn, target, blocked)
    verb = "blocked" if blocked else "allowed"
    await update.message.reply_text(
        f"{'🚫' if blocked else '✅'} {target} {verb}." if ok
        else f"No user with id {target}."
    )


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_access(update, blocked=False)


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_access(update, blocked=True)


def _prefs(conn, chat_id: int) -> dict:
    """This user's session settings, falling back to the global defaults."""
    return db.all_prefs(conn, chat_id, {
        "daily_count": config.DAILY_IDIOM_COUNT,
        "evening_count": config.EVENING_IDIOM_COUNT,
        "production_per_session": config.PRODUCTION_PER_SESSION,
    })


def _build_reask_questions(conn, chat_id: int, cap: int) -> list[Question]:
    """Pop up to `cap` missed idioms and rebuild each as the question type it is
    now due for.

    A miss holds the idiom on the modality it was missed on, so dispatching
    through build_one re-tests the skill that actually failed. Rebuilding every
    re-ask as a forward multiple-choice, as this used to, let a failed
    production question come back as a four-option pick.
    """
    questions: list[Question] = []
    for r in db.pop_reasks(conn, chat_id, cap):
        row = db.get_review_row(conn, r["idiom_id"], chat_id)
        if row is None:
            continue
        try:
            q = build_one(conn, row, chat_id)
        except ValueError:
            continue
        q.reask = True
        questions.append(q)
    return questions


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    chat_id = update.effective_chat.id
    n = 5
    if context.args:
        try:
            n = max(1, min(int(context.args[0]), 20))
        except ValueError:
            pass

    with db.connect(config.DB_PATH) as conn:
        # Prepend any pending re-asks — cap at n//3 so production/new questions
        # aren't starved when the re-ask queue is deep.
        reask_questions = _build_reask_questions(conn, chat_id, max(1, n // 3))
        reask_ids = [q.idiom_id for q in reask_questions]

        remaining = n - len(reask_questions)
        if remaining > 0:
            rows = db.build_daily_rows(
                conn, config.today_local(), remaining + 5, chat_id,
                extra_exclude_ids=reask_ids,
            )
            # Scale the cap to the size asked for, so /quiz 5 isn't all writing.
            per_session = _prefs(conn, chat_id)["production_per_session"]
            prod_cap = max(1, round(per_session * n / max(1, config.DAILY_IDIOM_COUNT)))
            prod_cap -= sum(1 for q in reask_questions if q.kind == "production")
            new_questions = build_questions_from_rows(
                conn, rows, chat_id, max_production=max(0, prod_cap)
            )[:remaining]
        else:
            new_questions = []

    questions = reask_questions + new_questions
    if not questions:
        await update.message.reply_text("No idioms to review right now. Ingest more PDFs!")
        return
    for q in questions:
        await _send_question(chat_id, q, context)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    user_id = update.effective_user.id
    with db.connect(config.DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM idioms").fetchone()[0]
        reviewed = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE user_id = ? AND repetitions > 0", (user_id,)
        ).fetchone()[0]
        row = conn.execute(
            "SELECT SUM(correct) as c, SUM(wrong) as w FROM reviews WHERE user_id = ?", (user_id,)
        ).fetchone()
        correct, wrong = row["c"] or 0, row["w"] or 0
        total_ans = correct + wrong
        accuracy = round(correct / total_ans * 100) if total_ans else 0
        weakest = conn.execute(
            """SELECT i.phrase, r.ease, r.correct, r.wrong
               FROM idioms i JOIN reviews r ON i.id = r.idiom_id
               WHERE r.user_id = ? AND r.repetitions > 0
               ORDER BY r.ease ASC LIMIT 5""",
            (user_id,),
        ).fetchall()

    lines = [
        f"*Idioms in DB:* {total}",
        f"*Reviewed:* {reviewed}",
        f"*Accuracy:* {accuracy}% ({correct}/{total_ans})\n",
        "*Weakest idioms:*",
    ]
    for w in weakest:
        lines.append(f"• {w['phrase']} (ease {w['ease']:.2f}, {w['correct']}✅/{w['wrong']}❌)")
    if not weakest:
        lines.append("_(none yet — start quizzing!)_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Work through the backlog of production questions that were never answered.

    The daily sets pick by due date, so a question left unanswered is not
    prioritised on the next run — it just sits. This drains that pile on demand,
    freshest first, a few at a time.
    """
    if await _deny_if_blocked(update):
        return
    from .quiz import build_production_question

    chat_id = update.effective_chat.id
    n = 5
    if context.args:
        try:
            n = max(1, min(int(context.args[0]), 20))
        except ValueError:
            pass

    with db.connect(config.DB_PATH) as conn:
        total = db.count_unanswered_production(conn, chat_id)
        rows = db.unanswered_production_idioms(conn, chat_id, n)
        questions = []
        for row in rows:
            idiom = db.get_review_row(conn, row["idiom_id"], chat_id)
            if idiom is None:
                continue
            try:
                questions.append(build_production_question(conn, idiom))
            except ValueError:
                continue

    if not questions:
        await update.message.reply_text(
            "Nothing pending — you've answered every production question sent to you. 🎉"
        )
        return

    remaining = max(0, total - len(questions))
    await update.message.reply_text(
        f"✍️ {len(questions)} unanswered production questions, most recent first. "
        f"{remaining} left after these — run /pending again for more."
    )
    for q in questions:
        await _send_question(chat_id, q, context)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or change this user's session settings."""
    if await _deny_if_blocked(update):
        return
    chat_id = update.effective_chat.id
    args = (update.message.text or "").split()[1:]

    with db.connect(config.DB_PATH) as conn:
        if len(args) >= 2 and args[0] in db.USER_PREFS and args[1].lstrip("-").isdigit():
            value = max(0, min(int(args[1]), 60))
            db.set_pref(conn, chat_id, args[0], value)
            await update.message.reply_text(f"✅ {args[0]} = {value}")
            return
        prefs = _prefs(conn, chat_id)

    lines = ["⚙️ *Your settings*", ""]
    for key, why in db.USER_PREFS.items():
        lines.append(f"`{key}` = *{prefs[key]}*")
        lines.append(f"  {why}")
    lines += ["", "Change one with `/set <name> <number>`",
              "e.g. `/set production_per_session 3`"]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revert the most recent content fix, whole."""
    if await _deny_if_blocked(update):
        return
    chat_id = update.effective_chat.id
    with db.connect(config.DB_PATH) as conn:
        fixes = db.last_content_fixes(conn, chat_id)
        if not fixes:
            await update.message.reply_text("Nothing to undo — no content fix on record.")
            return
        lines = ["↩️ Reverted", ""]
        for f in fixes:
            db.revert_content_fix(
                conn, f["id"], f["idiom_id"], f["field"], f["old_value"]
            )
            lines.append(f"*{f['field']}*")
            lines.append(f"− {f['new_value']}")
            lines.append(f"+ {f['old_value']}")
            lines.append("")
        lines.append(f"_from: {fixes[0]['complaint'][:120]}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_story(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    chat_id = update.effective_chat.id
    today = config.today_local().isoformat()
    with db.connect(config.DB_PATH) as conn:
        row = db.get_daily_story(conn, chat_id, today)

    if row and row["story_vi"]:
        with db.connect(config.DB_PATH) as conn:
            phrases_display = db.build_phrases_str(conn, row["idiom_ids"]) if row["idiom_ids"] else row["phrases"]
        await update.message.reply_text(
            f"📖 Today's story\n\n{phrases_display}\n\n{row['story']}\n\n🇻🇳 Bản dịch:\n{row['story_vi']}"
        )
        return

    # No story yet, or story exists but missing Vietnamese translation — generate now
    from anthropic import Anthropic
    from .examples import generate_daily_story, translate_to_vietnamese

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    if row:
        # Story exists but no Vietnamese — load idioms for translation context
        story = row["story"]
        idiom_ids_str = row["idiom_ids"] or ""
        with db.connect(config.DB_PATH) as conn:
            phrases = db.build_phrases_str(conn, idiom_ids_str) if idiom_ids_str else row["phrases"]
            story_idioms = db.get_idioms_by_ids(conn, idiom_ids_str) if idiom_ids_str else []
    else:
        with db.connect(config.DB_PATH) as conn:
            rows = db.due_idioms(conn, config.today_local(), config.DAILY_IDIOM_COUNT, chat_id)
            story_idioms = [
                {"id": r["id"], "phrase": r["phrase"], "meaning": r["meaning"],
                 "viet": r["vietnamese_equiv"] or "",
                 "register": _register_line(r, inline=True)}
                for r in rows
            ]
        if not story_idioms:
            await update.message.reply_text("No idioms available yet.")
            return
        idiom_ids_str = ",".join(str(i["id"]) for i in story_idioms)
        story = generate_daily_story(story_idioms, client)
        phrases = "\n".join(
            f'• "{i["phrase"]}"'
            + (f' — {i["viet"]}' if i["viet"] and i["viet"] != "—" else "")
            + i.get("register", "")
            for i in story_idioms
        )
        if not story:
            await update.message.reply_text("Couldn't generate a story right now, try again.")
            return

    story_vi = translate_to_vietnamese(story, client, idioms=story_idioms)
    with db.connect(config.DB_PATH) as conn:
        db.save_daily_story(conn, chat_id, today, story, phrases, story_vi, idiom_ids_str)
    vi_section = f"\n\n🇻🇳 Bản dịch:\n{story_vi}" if story_vi else ""
    await update.message.reply_text(f"📖 Today's story\n\n{phrases}\n\n{story}{vi_section}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    await update.message.reply_text(
        "/start  — register\n"
        "/quiz   — get 5 questions now\n"
        "/quiz N — get N questions (max 20)\n"
        "/pending — unanswered production questions (/pending N for more)\n"
        "/story  — today's idiom story\n"
        "/stats  — see your progress\n"
        "/skipped — list idioms you've marked as known\n"
        "/unskip <id or phrase> — bring a skipped idiom back\n"
        "/set    — your session settings (size, production limit)\n"
        "/help   — this message\n\n"
        "Spot a mistake? Reply to the question with ! and what's wrong —\n"
        "e.g. \"! the Vietnamese is a literal gloss, not a real idiom\".\n"
        "\"fix:\" and \"sai:\" work too. I'll correct the entry and show the diff,\n"
        "or explain why nothing changed. /undo reverts my last fix.\n\n"
        "Tone & register key — when an idiom is sayable:\n"
        + register_legend()
    )


async def cmd_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    chat_id = update.effective_chat.id
    with db.connect(config.DB_PATH) as conn:
        rows = db.list_skipped(conn, chat_id)
    if not rows:
        await update.message.reply_text(
            "You haven't skipped any idioms yet. Tap ⏭ I know this on any quiz to mark one."
        )
        return
    lines = [f"⏭ *Skipped idioms ({len(rows)}):*", ""]
    for r in rows[:50]:
        viet = r["vietnamese_equiv"] or ""
        viet_part = f" — {viet}" if viet and viet != "—" else ""
        lines.append(f"`{r['id']:>5}`  {r['phrase']}{viet_part}")
    if len(rows) > 50:
        lines.append(f"\n_…and {len(rows) - 50} more._")
    lines.append("\nUse `/unskip <id>` or `/unskip <phrase>` to bring one back.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_unskip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /unskip <id>  or  /unskip <exact phrase>\nSee /skipped for the list."
        )
        return
    arg = " ".join(context.args).strip()
    with db.connect(config.DB_PATH) as conn:
        idiom_id: int | None = None
        try:
            idiom_id = int(arg)
        except ValueError:
            row = db.find_idiom_by_phrase(conn, arg)
            if row:
                idiom_id = row["id"]
        if idiom_id is None:
            await update.message.reply_text(f"No idiom found for: {arg}")
            return
        idiom = db.get_idiom(conn, idiom_id)
        ok = db.unmark_skipped(conn, chat_id, idiom_id)
    if ok and idiom:
        await update.message.reply_text(f"✅ Brought back: {idiom['phrase']}")
    else:
        await update.message.reply_text(f"Couldn't unskip #{idiom_id}.")


def _register_line(idiom, inline: bool = False) -> str:
    """Tone-and-register tags, or "" when the idiom is untagged.

    Shown wherever an idiom is taught rather than merely asked, since the tags
    answer when the phrase is sayable — the thing meaning alone does not give
    you. `inline` keeps them on the current line, for bulleted phrase lists;
    otherwise they get a line of their own under the meaning.
    """
    from .examples import format_register
    try:
        tags = format_register(idiom["register"])
    except (IndexError, KeyError):
        return ""
    if not tags:
        return ""
    return f"  {tags}" if inline else f"\n{tags}"


def _reveal_context(idiom, cached) -> str:
    """Trailing sentence of an answer reveal.

    Uses the question's own stem with the blank filled in, so the answer reads
    against the sentence just asked; falls back to a stored example when the
    question had no blank, as reverse and production questions do not.
    """
    stem, kind = (cached[0], cached[1]) if cached else (None, "forward")
    if stem and kind in ("forward", "completion"):
        return "\n\n" + stem.replace("___", "[" + idiom["phrase"] + "]")
    return "\n\n" + (idiom["story"] or idiom["example"] or idiom["meaning"])


async def _set_markup(query, markup) -> None:
    """Replace a message's buttons, ignoring "not modified".

    Telegram rejects an edit that changes nothing, which a repeat tap produces.
    That is not a failure worth raising — the markup already says what we want.
    """
    from telegram.error import BadRequest
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 4 or parts[0] != "ans":
        await _set_markup(query, None)
        return

    idiom_id = int(parts[1])
    chosen = int(parts[2])
    correct_index = int(parts[3])

    chat_id = query.message.chat_id
    with db.connect(config.DB_PATH) as conn:
        # Claim first: a second tap must not grade the same question twice.
        if not db.claim_question(conn, chat_id, query.message.message_id):
            return
        idiom = db.get_idiom(conn, idiom_id)
        quality = 5 if chosen == correct_index else 2
        db.apply_review(conn, idiom_id, quality, chat_id)
        if chosen != correct_index:
            db.add_reask(conn, chat_id, idiom_id)

    phrase = idiom["phrase"]
    meaning = idiom["meaning"]
    viet = idiom["vietnamese_equiv"] or ""
    viet_line = f"\n🇻🇳 {viet}" if viet and viet != "—" else ""

    # Retrieve the original question stem so the answer shows the same sentence
    cached = _stem_cache.pop(query.message.message_id, None)
    options = cached[2] if cached else None
    context_line = _reveal_context(idiom, cached)

    register_line = _register_line(idiom)
    if chosen == correct_index:
        reply = f"✅ Correct!\n\n{phrase} — {meaning}{viet_line}{register_line}{context_line}"
    else:
        chosen_label = options[chosen] if options and chosen < len(options) else LETTERS[chosen]
        reply = (
            f"❌ You chose: {chosen_label}\n"
            f"✅ Answer: {phrase}\n\n"
            f"{meaning}{viet_line}{register_line}{context_line}"
        )

    await _set_markup(query, _skip_only_keyboard(idiom_id))
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=reply,
        reply_to_message_id=query.message.message_id,
    )


async def handle_dunno(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reveal the answer to a question the user cannot answer.

    Graded as a miss, exactly like a wrong choice: guessing at random to move on
    would otherwise feed SM-2 correct answers the user never actually knew.
    """
    if await _deny_if_blocked(update):
        return
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 2 or parts[0] != "dunno":
        await query.answer()
        return
    await query.answer()

    idiom_id = int(parts[1])
    chat_id = query.message.chat_id
    message_id = query.message.message_id

    with db.connect(config.DB_PATH) as conn:
        # Same claim as handle_answer: giving up counts as one attempt, and a
        # question already answered must not be re-graded as a miss.
        if not db.claim_question(conn, chat_id, message_id):
            return
        idiom = db.get_idiom(conn, idiom_id)
        pending = db.get_production_pending(conn, chat_id, message_id)
        # Follow-up production turns are bonus practice, so they leave the SM-2
        # state alone — the same rule _evaluate_production applies.
        first_turn = pending is None or pending["turn_number"] <= 1
        if first_turn:
            db.apply_review(conn, idiom_id, 2, chat_id)
            db.add_reask(conn, chat_id, idiom_id)
        # The question is closed now, so stop waiting for a typed sentence —
        # including any older unanswered copy of the same idiom.
        db.clear_production_pending_upto(conn, chat_id, idiom_id, message_id)

    if idiom is None:
        return

    viet = idiom["vietnamese_equiv"] or ""
    viet_line = f"\n🇻🇳 {viet}" if viet and viet != "—" else ""
    context_line = _reveal_context(idiom, _stem_cache.pop(message_id, None))
    reply = (
        f"🤷 Answer: {idiom['phrase']}\n\n"
        f"{idiom['meaning']}{viet_line}{_register_line(idiom)}{context_line}"
    )

    await _set_markup(query, _skip_only_keyboard(idiom_id))
    await context.bot.send_message(
        chat_id=chat_id,
        text=reply,
        reply_to_message_id=message_id,
    )


async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) != 2 or parts[0] != "skip":
        await query.answer()
        return
    idiom_id = int(parts[1])
    chat_id = query.message.chat_id

    with db.connect(config.DB_PATH) as conn:
        idiom = db.get_idiom(conn, idiom_id)
        updated = db.mark_skipped(conn, chat_id, idiom_id)

    if not updated:
        await query.answer("Couldn't skip — try /start first.", show_alert=True)
        return

    phrase = idiom["phrase"] if idiom else f"#{idiom_id}"
    await query.answer(f"Skipped: {phrase}. Use /unskip {idiom_id} to undo.")
    await _set_markup(query, None)
    # Also clear from production cache and re-ask queue so it doesn't keep coming back
    with db.connect(config.DB_PATH) as conn:
        db.clear_production_pending(conn, chat_id, query.message.message_id)
        conn.execute(
            "DELETE FROM reask_queue WHERE chat_id = ? AND idiom_id = ?",
            (chat_id, idiom_id),
        )


def _build_story_for(conn, chat_id: int, today, client) -> tuple[str, str, str]:
    """Generate one user's daily story. Returns (story, vietnamese, phrase_list).

    Each user gets their own story built from their own pipeline. The story is
    what introduces an idiom before it is ever quizzed, and the story-introduced
    quiz bucket reads back the stories that user was actually sent, so sharing
    one story across users would seed everyone's pipeline from one person's
    progress.

    Uses a SMALLER idiom set than the quiz so the rendered message stays under
    Telegram's 4096-char per-message limit, and reserves slots for FRESH phase-0
    idioms — ones this user has never met in a story — to keep the introduction
    pipeline flowing.
    """
    from .examples import generate_daily_story, translate_to_vietnamese

    fresh_slots = min(5, config.STORY_IDIOM_COUNT // 3)
    remaining_slots = config.STORY_IDIOM_COUNT - fresh_slots
    fresh_rows = db.never_in_story_idioms(conn, fresh_slots, [], chat_id)
    fresh_ids = [r["id"] for r in fresh_rows]
    pipeline_rows = db.build_daily_rows(
        conn, today, remaining_slots, chat_id, extra_exclude_ids=fresh_ids,
    )
    story_rows = fresh_rows + pipeline_rows
    if not story_rows:
        return "", "", ""

    story_idioms = [
        {"id": r["id"], "phrase": r["phrase"], "meaning": r["meaning"],
         "viet": r["vietnamese_equiv"] or "",
         "register": _register_line(r, inline=True)}
        for r in story_rows
    ]
    phrases_str = "\n".join(
        f'• "{i["phrase"]}"'
        + (f' — {i["viet"]}' if i["viet"] and i["viet"] != "—" else "")
        + i.get("register", "")
        for i in story_idioms
    )
    story = generate_daily_story(story_idioms, client)
    if not story:
        return "", "", phrases_str
    story_vi = translate_to_vietnamese(story, client, idioms=story_idioms)
    db.save_daily_story(
        conn, chat_id, today.isoformat(), story, phrases_str, story_vi,
        ",".join(str(i["id"]) for i in story_idioms),
    )
    return story, story_vi, phrases_str


async def send_daily_quiz(application: Application) -> None:
    from anthropic import Anthropic
    from .examples import generate_daily_story, translate_to_vietnamese

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    today = config.today_local()

    with db.connect(config.DB_PATH) as conn:
        users = db.all_users(conn)

    if not users:
        return

    # Look up what was sent in the last 2 days for each user to avoid same-set repeats
    recent_cutoff = (today - timedelta(days=2)).isoformat()

    for chat_id in users:
        # Build the quiz first, then send each section in its own try block —
        # if IoTD or the story fails (e.g. too long), the questions still go out.
        try:
            with db.connect(config.DB_PATH) as conn:
                iotd = db.weakest_idiom(conn, chat_id)
                # Exclude idioms sent to this user in the last 2 days so morning doesn't
                # rehash the same shortfall picks. Falls back to any if pool goes thin.
                recent_sent = [
                    r[0] for r in conn.execute(
                        "SELECT idiom_id FROM question_sent WHERE chat_id = ? AND sent_date >= ?",
                        (chat_id, recent_cutoff),
                    )
                ]
                # Missed idioms lead the set. They take slots from the total
                # rather than adding to it, so the session length is unchanged.
                prefs = _prefs(conn, chat_id)
                total = prefs["daily_count"]
                reasks = _build_reask_questions(conn, chat_id, max(1, total // 3))
                remaining = total - len(reasks)
                rows = db.build_daily_rows(
                    conn, today, remaining + 10, chat_id,
                    extra_exclude_ids=recent_sent + [q.idiom_id for q in reasks],
                )
                prod_left = max(
                    0, prefs["production_per_session"]
                    - sum(1 for q in reasks if q.kind == "production")
                )
                questions = reasks + build_questions_from_rows(
                    conn, rows, chat_id, max_production=prod_left
                )
            questions = questions[:total]
        except Exception as e:
            logger.error("Daily quiz: build failed for user %s: %s", chat_id, e)
            continue

        # Built per user, in its own try block: a story failure must not cost
        # this user their quiz.
        daily_story = story_vi = phrases_str = ""
        try:
            with db.connect(config.DB_PATH) as conn:
                daily_story, story_vi, phrases_str = _build_story_for(
                    conn, chat_id, today, client
                )
        except Exception as e:
            logger.error("Daily story: build failed for user %s: %s", chat_id, e)

        if not questions:
            logger.warning("Daily quiz: no questions for user %s", chat_id)
            continue

        if iotd:
            try:
                iotd_phrase = iotd["phrase"]
                iotd_meaning = iotd["meaning"]
                iotd_story_text = iotd["story"] or iotd["example"] or ""
                viet = iotd["vietnamese_equiv"] or ""
                viet_line = f"\n🇻🇳 {viet}" if viet and viet != "—" else ""
                story_line = f"\n\n{iotd_story_text}" if iotd_story_text else ""
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(f"🌟 Idiom of the Day\n\n{iotd_phrase}\n{iotd_meaning}"
                          f"{viet_line}{_register_line(iotd)}{story_line}"),
                )
                with db.connect(config.DB_PATH) as conn:
                    db.mark_idiom_of_the_day(conn, chat_id, iotd["id"], today)
            except Exception as e:
                logger.warning("Daily quiz: IoTD send failed for %s: %s", chat_id, e)

        if daily_story:
            try:
                vi_section = f"\n\n🇻🇳 Bản dịch:\n{story_vi}" if story_vi else ""
                full_text = f"📖 Today's story\n\n{phrases_str}\n\n{daily_story}{vi_section}"
                # Split into chunks under Telegram's 4096-char limit, preferring paragraph breaks.
                for chunk in _split_for_telegram(full_text, 3900):
                    await application.bot.send_message(chat_id=chat_id, text=chunk)
            except Exception as e:
                logger.warning("Daily quiz: story send failed for %s: %s", chat_id, e)

        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"Good morning! Now let's test your recall 🌅 ({len(questions)} questions)",
            )
            for q in questions:
                await _send_question(chat_id, q, type("ctx", (), {"bot": application.bot})())
        except Exception as e:
            logger.error("Daily quiz: question send failed for %s: %s", chat_id, e)


def _split_for_telegram(text: str, limit: int = 3900) -> list[str]:
    """Split a long string into chunks ≤ limit, preferring paragraph boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Try to split on the last paragraph break before the limit
        cut = remaining.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def send_evening_quiz(application: Application) -> None:
    """Lighter evening session: pure quiz, no story, no IoTD. Used for spaced repetition."""
    today = config.today_local()
    with db.connect(config.DB_PATH) as conn:
        users = db.all_users(conn)
    if not users:
        return

    today_str = today.isoformat()
    for chat_id in users:
        try:
            with db.connect(config.DB_PATH) as conn:
                # Skip idioms already SENT today (regardless of whether user answered).
                # Covers morning quiz + any /q, even if morning is still un-answered.
                sent_today = db.get_sent_today(conn, chat_id, today_str)
                prefs = _prefs(conn, chat_id)
                total = prefs["evening_count"]
                reasks = _build_reask_questions(conn, chat_id, max(1, total // 3))
                remaining = total - len(reasks)
                rows = db.build_daily_rows(
                    conn, today, remaining + 10, chat_id,
                    extra_exclude_ids=sent_today + [q.idiom_id for q in reasks],
                )
                prod_left = max(
                    0, prefs["production_per_session"]
                    - sum(1 for q in reasks if q.kind == "production")
                )
                questions = reasks + build_questions_from_rows(
                    conn, rows, chat_id, max_production=prod_left
                )
            questions = questions[:total]
            if not questions:
                continue
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"Evening practice 🌙 ({len(questions)} questions)",
            )
            for q in questions:
                await _send_question(chat_id, q, type("ctx", (), {"bot": application.bot})())
        except Exception as e:
            logger.error("Failed to send evening quiz to %s: %s", chat_id, e)


async def send_weekly_review(application: Application) -> None:
    from anthropic import Anthropic
    from .examples import generate_daily_story, translate_to_vietnamese

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    with db.connect(config.DB_PATH) as conn:
        users = db.all_users(conn)

    for chat_id in users:
        try:
            with db.connect(config.DB_PATH) as conn:
                rows = db.weak_idioms_this_week(conn, 10, chat_id)
                if not rows:
                    continue
                questions = build_questions_from_rows(conn, rows, chat_id)

            story_idioms = [
                {"id": r["id"], "phrase": r["phrase"], "meaning": r["meaning"],
                 "viet": r["vietnamese_equiv"] or "",
                 "register": _register_line(r, inline=True)}
                for r in rows
            ]
            bullet_lines = []
            for r in rows:
                total_ans = (r["correct"] or 0) + (r["wrong"] or 0)
                pct = round((r["correct"] or 0) / total_ans * 100) if total_ans else 0
                bullet_lines.append(f"• {r['phrase']} ({pct}% correct)")
            bullets = "\n".join(bullet_lines)

            phrases_str = "\n".join(
                f'• "{i["phrase"]}"'
                + (f' — {i["viet"]}' if i["viet"] and i["viet"] != "—" else "")
                + i.get("register", "")
                for i in story_idioms
            )
            daily_story = ""
            story_vi = ""
            try:
                daily_story = generate_daily_story(story_idioms, client)
                if daily_story:
                    story_vi = translate_to_vietnamese(daily_story, client, idioms=story_idioms)
            except Exception as e:
                logger.error("Failed to generate weekly review story for %s: %s", chat_id, e)

            await application.bot.send_message(
                chat_id=chat_id,
                text=f"📅 Weekly Review — your toughest idioms this week:\n\n{bullets}",
            )
            if daily_story:
                vi_section = f"\n\n🇻🇳 Bản dịch:\n{story_vi}" if story_vi else ""
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"📖 Weekly story\n\n{phrases_str}\n\n{daily_story}{vi_section}",
                )
            for q in questions:
                await _send_question(chat_id, q, type("ctx", (), {"bot": application.bot})())
        except Exception as e:
            logger.error("Failed to send weekly review to %s: %s", chat_id, e)


async def handle_direct_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    msg = update.message
    if not msg or not msg.text:
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")

    from anthropic import Anthropic
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    text = msg.text.strip()
    is_single_word = len(text.split()) == 1 and text.isalpha()

    if is_single_word:
        prompt = (
            f"Look up the English word: {text}\n\n"
            "Reply in exactly this format (no extra lines):\n"
            "🇻🇳 <Vietnamese translation(s), comma-separated if multiple>\n\n"
            "📖 <English definition, 1-2 sentences, plain English>\n\n"
            "💬 <one short example sentence using the word>"
        )
        system = "You are a concise bilingual dictionary (English–Vietnamese). Follow the format exactly."
        max_tokens = 200
    else:
        prompt = text
        system = (
            "You are a friendly English idiom learning assistant embedded in a Telegram bot. "
            "Answer questions about English idioms, phrases, grammar, or anything language-related. "
            "Keep responses concise and conversational, under 200 words."
        )
        max_tokens = 600

    resp = client.messages.create(
        model=config.CONTENT_MODEL,
        max_tokens=config.reply_budget(max_tokens),
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )

    answer = config.response_text(resp) or "Sorry, I couldn't process that."
    await msg.reply_text(answer)


MULTI_TURN_MAX = 2  # Total production turns per idiom drill (initial + N-1 follow-ups)


async def _send_production_followup(chat_id: int, idiom_id: int, phrase: str,
                                     turn_number: int, used_situations: list[str],
                                     bot) -> None:
    """Send a follow-up production question using a NEW concrete situation."""
    from .quiz import _generate_situation

    with db.connect(config.DB_PATH) as conn:
        idiom = db.get_idiom(conn, idiom_id)
    meaning = idiom["meaning"] if idiom else ""
    viet = idiom["vietnamese_equiv"] if idiom else ""
    viet_line = f"\n🇻🇳 {viet}" if viet and viet != "—" else ""

    situation = _generate_situation(phrase, meaning, used_situations)
    if situation is None:
        # Skip the bonus turn rather than ask one with no usable situation. The
        # first turn already scored; extra drills are optional.
        logger.warning("Skipping follow-up turn for %r: no situation", phrase)
        return

    stem = (
        f"🔁 Turn {turn_number}/{MULTI_TURN_MAX} — same idiom, new situation.\n\n"
        f"Meaning: {meaning}{viet_line}\n\n"
        f"Situation: {situation}\n\n"
        "Recall the idiom that fits and use it in a sentence.\n\n"
        "Reply to this message with your sentence 👇"
    )
    sent = await bot.send_message(
        chat_id=chat_id, text=stem,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🤷 Don't know", callback_data=f"dunno:{idiom_id}"),
            _skip_button(idiom_id),
        ]]),
    )
    new_used = "|".join(used_situations + [situation])
    with db.connect(config.DB_PATH) as conn:
        db.save_production_pending(
            conn, chat_id, sent.message_id, idiom_id, phrase,
            turn_number=turn_number, used_situations=new_used,
        )
        db.log_question_msg(conn, chat_id, sent.message_id, idiom_id, "production")


async def _evaluate_production(update: Update, context: ContextTypes.DEFAULT_TYPE, prod: dict, user_sentence: str) -> bool:
    """Return True if grading succeeded, False on transient API failure."""
    await context.bot.send_chat_action(chat_id=update.message.chat_id, action="typing")

    import anthropic
    from anthropic import Anthropic
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    idiom_id = prod["idiom_id"]
    phrase = prod["phrase"]
    user_id = prod["user_id"]
    turn_number = prod.get("turn_number", 1)
    used_situations_raw = prod.get("used_situations", "") or ""
    used_situations = [s for s in used_situations_raw.split("|") if s]

    with db.connect(config.DB_PATH) as conn:
        idiom = db.get_idiom(conn, idiom_id)
    meaning = idiom["meaning"] if idiom else ""

    try:
        resp = client.messages.create(
            model=config.GRADER_MODEL,
            max_tokens=config.reply_budget(300),
            messages=[{"role": "user", "content": PRODUCTION_EVAL_PROMPT.format(
                phrase=phrase, meaning=meaning, sentence=user_sentence,
            )}],
        )
    except anthropic.BadRequestError as e:
        err_msg = str(e)
        if "credit balance" in err_msg.lower():
            user_msg = "⚠️ Couldn't grade — the API account is out of credits. Top up and reply again."
        else:
            user_msg = f"⚠️ Couldn't grade right now (API error). Reply again to retry."
        logger.warning("Production eval failed: %s", e)
        await update.message.reply_text(user_msg)
        return False
    except (anthropic.APIConnectionError, anthropic.APIStatusError, anthropic.RateLimitError) as e:
        logger.warning("Production eval transient error: %s", e)
        await update.message.reply_text(
            "⚠️ Couldn't reach the grader right now. Reply again in a moment to retry."
        )
        return False

    raw = config.response_text(resp)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    result_line = lines[0].upper() if lines else ""

    # A reply that carries no verdict is a failed call, not a wrong answer.
    # Reasoning models can spend the whole budget thinking and return an empty
    # text block with stop_reason max_tokens; treating that as INCORRECT scored
    # a miss against the user and showed them a bare ❌ with no feedback.
    if not (result_line.startswith("CORRECT") or result_line.startswith("INCORRECT")):
        logger.warning(
            "Production eval unusable for idiom %s: stop=%s out_tokens=%s raw=%r",
            idiom_id, resp.stop_reason, resp.usage.output_tokens, raw[:200],
        )
        await update.message.reply_text(
            "⚠️ The grader didn't come back with a verdict. Reply again to retry — "
            "this doesn't count against you."
        )
        return False

    feedback = "\n".join(lines[1:]) if len(lines) > 1 else ""
    correct = result_line.startswith("CORRECT")
    # Only advance SM-2 state on the FIRST turn; follow-up drills should not
    # push the SRS interval further (extra reps beyond the first are bonus practice).
    is_first_turn = turn_number <= 1

    with db.connect(config.DB_PATH) as conn:
        if is_first_turn:
            quality = 5 if correct else 2
            db.apply_review(conn, idiom_id, quality, user_id)
            if not correct:
                db.add_reask(conn, update.message.chat_id, idiom_id)
        # Logged for every turn, and before any of it can be lost: grading
        # deletes the cache row, so the sentence and the verdict on it would
        # otherwise leave no trace to review.
        db.log_production_answer(
            conn, update.message.chat_id, idiom_id, phrase,
            used_situations[-1] if used_situations else "",
            user_sentence, correct, feedback, turn_number,
        )

    icon = "✅" if correct else "❌"
    if not feedback:
        feedback = ("Correct." if correct
                    else f'Not quite — the idiom is "{phrase}".')
    register_line = _register_line(idiom) if idiom else ""
    await update.message.reply_text(f"{icon} {feedback}{register_line}")

    # Multi-turn drill: chain another situation if correct AND we haven't hit the cap.
    # For graduated (phase >= 3) idioms only — boot items are still learning basics.
    if correct and turn_number < MULTI_TURN_MAX:
        rev = None
        with db.connect(config.DB_PATH) as conn:
            rev = conn.execute(
                "SELECT boot_phase FROM reviews WHERE user_id = ? AND idiom_id = ?",
                (user_id, idiom_id),
            ).fetchone()
        if rev and rev["boot_phase"] >= 3:
            try:
                await _send_production_followup(
                    update.message.chat_id, idiom_id, phrase,
                    turn_number + 1, used_situations, context.bot,
                )
            except Exception as e:
                logger.warning("Multi-turn followup send failed: %s", e)

    return True


FEEDBACK_PREFIXES = ("!", "fix:", "sai:")


def _feedback_body(text: str) -> str | None:
    """The complaint in a feedback reply, or None if this is not one.

    "sai" is Vietnamese for wrong, so a report can be raised in either language.
    """
    stripped = (text or "").strip()
    for prefix in FEEDBACK_PREFIXES:
        if stripped.lower().startswith(prefix):
            return stripped[len(prefix):].strip()
    return None


async def _handle_content_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   idiom_id: int, complaint: str,
                                   question_text: str = "") -> None:
    """Send a reported content problem to Claude and apply any correction.

    `question_text` is the message the user replied to. Without it a complaint
    about the question itself — a missing situation, a confusing prompt — looks
    like a complaint about the dictionary entry, and the editor invents a field
    change to satisfy it.
    """
    import anthropic
    from anthropic import Anthropic

    msg = update.message
    chat_id = msg.chat_id
    if not complaint:
        await msg.reply_text(
            "Tell me what's wrong after the prefix — e.g. "
            "\"! the Vietnamese is a word-for-word gloss, not a real idiom\"."
        )
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    with db.connect(config.DB_PATH) as conn:
        idiom = db.get_idiom(conn, idiom_id)
    if idiom is None:
        await msg.reply_text("Couldn't find that idiom any more.")
        return

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=config.CONTENT_MODEL,
            max_tokens=config.reply_budget(1500),
            messages=[{"role": "user", "content": CONTENT_FIX_PROMPT.format(
                question=question_text.strip() or "(not available)",
                phrase=idiom["phrase"],
                meaning=idiom["meaning"] or "",
                vietnamese_equiv=idiom["vietnamese_equiv"] or "",
                example=idiom["example"] or "",
                story=idiom["story"] or "",
                complaint=complaint,
            )}],
        )
    except (anthropic.APIError, anthropic.APIConnectionError) as e:
        logger.warning("Content fix failed for idiom %s: %s", idiom_id, e)
        await msg.reply_text("⚠️ Couldn't reach the editor right now. Try again in a moment.")
        return

    raw = config.response_text(resp)
    updates, note = _parse_content_fix(raw)

    if not updates:
        await msg.reply_text(f"📝 No change made.\n\n{note or raw}")
        return

    lines = [f"✏️ Updated *{idiom['phrase']}*", ""]
    with db.connect(config.DB_PATH) as conn:
        for field, value in updates.items():
            old_value = idiom[field] or ""
            if value == old_value:
                continue
            db.apply_content_fix(conn, idiom_id, field, value)
            db.log_content_fix(conn, chat_id, idiom_id, complaint, field, old_value, value)
            lines.append(f"*{field}*")
            lines.append(f"− {old_value}")
            lines.append(f"+ {value}")
            lines.append("")
    if note:
        lines.append(note)
    await msg.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_blocked(update):
        return
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    # Only handle replies to the bot's own messages
    if not msg.reply_to_message.from_user or msg.reply_to_message.from_user.id != context.bot.id:
        return

    replied_id = msg.reply_to_message.message_id
    chat_id = msg.chat_id

    # A feedback prefix wins over every other reading of the reply: on a
    # production question the same text would otherwise be graded as an answer.
    complaint = _feedback_body(msg.text or "")
    if complaint is not None:
        with db.connect(config.DB_PATH) as conn:
            anchor = db.get_question_msg(conn, chat_id, replied_id)
        if anchor is None:
            await msg.reply_text(
                "I can't tell which idiom that's about — reply to the question "
                "message itself and I'll fix it."
            )
            return
        await _handle_content_feedback(
            update, context, anchor["idiom_id"], complaint,
            question_text=msg.reply_to_message.text or "",
        )
        return

    # Check if this is a reply to a production question — peek DB, then clear only on success.
    with db.connect(config.DB_PATH) as conn:
        pending = db.get_production_pending(conn, chat_id, replied_id)
    if pending:
        prod = {
            "idiom_id": pending["idiom_id"],
            "phrase": pending["phrase"],
            "user_id": chat_id,
            "turn_number": pending["turn_number"] if "turn_number" in pending.keys() else 1,
            "used_situations": pending["used_situations"] if "used_situations" in pending.keys() else "",
        }
        ok = await _evaluate_production(update, context, prod, msg.text or "")
        if ok:
            with db.connect(config.DB_PATH) as conn:
                db.clear_production_pending_upto(
                    conn, chat_id, pending["idiom_id"], replied_id
                )
                # Settle the message so "Don't know" on it can't re-grade as a
                # miss. Not claimed before grading: a transient grader failure
                # invites the user to reply again.
                db.mark_question_answered(conn, chat_id, replied_id)
        return

    user_question = msg.text or ""
    bot_context = msg.reply_to_message.text or ""

    # Guard: if the user's reply LOOKS like a production-question answer (short sentence,
    # not a question) AND the message they replied to LOOKS like a production question
    # (contains the "Use it in a sentence!" marker), but the cache missed — tell them
    # to reply to the current quiz. Prevents the tutor from hallucinating a verdict.
    looks_like_answer = (
        len(user_question.split()) >= 3
        and "?" not in user_question
        and len(user_question) < 300
    )
    replied_looks_like_quiz = (
        "Use it in a sentence" in bot_context
        or "Recall the idiom" in bot_context
    )
    if looks_like_answer and replied_looks_like_quiz:
        await msg.reply_text(
            "I can't match this to a production question — the quiz message it came from "
            "isn't in my cache anymore. Please reply to a fresh production question "
            "(from your latest quiz) so I can grade it against the right idiom."
        )
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")

    from anthropic import Anthropic
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    resp = client.messages.create(
        model=config.CONTENT_MODEL,
        max_tokens=config.reply_budget(300),
        system=(
            "You are an English idiom tutor in a Telegram quiz bot. The user replied to a bot message "
            "with a follow-up. Answer in the SAME compact style as the quiz grader:\n"
            "- Max 4 short lines total.\n"
            "- One line of direct answer or verdict.\n"
            "- If an idiom is being discussed, include 'Example: <one sentence using the idiom>'.\n"
            "- Optional 'Similar: <1-3 related idioms>' line.\n"
            "- No emojis. No 'Great!'/'Well done!' filler. No bullet-list expansions. No headings.\n"
            "- Under 60 words."
        ),
        messages=[{
            "role": "user",
            "content": f"Context from the bot:\n{bot_context}\n\nUser reply:\n{user_question}",
        }],
    )

    answer = config.response_text(resp) or "Sorry, I couldn't process that."
    await msg.reply_text(answer)


def run(db_path: str) -> None:
    logging.basicConfig(level=logging.INFO)

    scheduler = AsyncIOScheduler(timezone=config.TZ)

    async def on_startup(app: Application) -> None:
        await app.bot.set_my_commands([
            BotCommand("q", "Quick quiz (alias /quiz)"),
            BotCommand("quiz", "Get 5 questions now (/quiz N for more)"),
            BotCommand("p", "Unanswered production questions (alias /pending)"),
            BotCommand("pending", "Work through unanswered production questions"),
            BotCommand("undo", "Revert my last content fix"),
            BotCommand("set", "Show or change your session settings"),
            BotCommand("s", "Today's story (alias /story)"),
            BotCommand("story", "Today's idiom story"),
            BotCommand("stats", "See your progress"),
            BotCommand("skipped", "List skipped idioms"),
            BotCommand("unskip", "Bring a skipped idiom back"),
            BotCommand("h", "Help (alias /help)"),
            BotCommand("help", "Command list"),
        ])
        scheduler.add_job(
            send_daily_quiz,
            "cron",
            hour=config.DAILY_HOUR,
            minute=0,
            kwargs={"application": app},
        )
        scheduler.add_job(
            send_evening_quiz,
            "cron",
            hour=config.EVENING_HOUR,
            minute=0,
            kwargs={"application": app},
        )
        scheduler.add_job(
            send_weekly_review,
            "cron",
            day_of_week="sat",
            hour=8,
            minute=0,
            kwargs={"application": app},
        )
        scheduler.start()
        logger.info(
            "Scheduler started. Morning quiz %d:00, evening quiz %d:00 %s",
            config.DAILY_HOUR, config.EVENING_HOUR, config.TZ,
        )

    async def on_shutdown(app: Application) -> None:
        scheduler.shutdown(wait=False)

    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler(["quiz", "q"], cmd_quiz))
    application.add_handler(CommandHandler(["pending", "p"], cmd_pending))
    application.add_handler(CommandHandler("undo", cmd_undo))
    application.add_handler(CommandHandler("set", cmd_set))
    application.add_handler(CommandHandler(["story", "s"], cmd_story))
    application.add_handler(CommandHandler(["stats", "stat"], cmd_stats))
    application.add_handler(CommandHandler(["help", "h"], cmd_help))
    application.add_handler(CommandHandler("skipped", cmd_skipped))
    application.add_handler(CommandHandler("unskip", cmd_unskip))
    application.add_handler(CommandHandler("users", cmd_users))
    application.add_handler(CommandHandler("allow", cmd_allow))
    application.add_handler(CommandHandler("block", cmd_block))
    application.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^ans:"))
    application.add_handler(CallbackQueryHandler(handle_dunno, pattern=r"^dunno:"))
    application.add_handler(CallbackQueryHandler(handle_skip, pattern=r"^skip:"))
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, handle_user_reply))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.REPLY & ~filters.COMMAND, handle_direct_message))

    application.run_polling(drop_pending_updates=True)
