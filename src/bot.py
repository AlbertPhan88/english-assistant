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
from .quiz import Question, build_daily_set, build_question, build_question_from_story, build_questions_from_rows, build_reverse_question

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


def _keyboard(q: Question) -> InlineKeyboardMarkup | None:
    skip_btn = InlineKeyboardButton("⏭ I know this", callback_data=f"skip:{q.idiom_id}")
    if q.kind == "production":
        return InlineKeyboardMarkup([[skip_btn]])
    buttons = [
        InlineKeyboardButton(
            LETTERS[i],
            callback_data=f"ans:{q.idiom_id}:{i}:{q.correct_index}"
        )
        for i in range(len(q.options))
    ]
    return InlineKeyboardMarkup([buttons, [skip_btn]])


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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    with db.connect(config.DB_PATH) as conn:
        db.register_user(conn, user.id, user.username)
    await update.message.reply_text(
        f"Hi {user.first_name}!\n\n"
        "I'll send you 15 idiom quizzes every day at 6 AM.\n"
        "Type /quiz anytime for an extra set, /stats to see your progress."
    )


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        reask_cap = max(1, n // 3)
        reask_rows = db.pop_reasks(conn, chat_id, reask_cap)
        reask_questions = []
        seen_ids: set[int] = set()
        for r in reask_rows:
            if r["idiom_id"] in seen_ids:
                continue
            idiom = db.get_idiom(conn, r["idiom_id"])
            if idiom:
                try:
                    q = build_question(conn, idiom, chat_id)
                except ValueError:
                    try:
                        q = build_reverse_question(conn, idiom, chat_id)
                    except ValueError:
                        continue
                q.reask = True
                reask_questions.append(q)
                seen_ids.add(r["idiom_id"])

        remaining = n - len(reask_questions)
        if remaining > 0:
            rows = db.build_daily_rows(conn, config.today_local(), remaining + 5, chat_id)
            candidates = build_questions_from_rows(conn, rows, chat_id)
            new_questions = []
            for q in candidates:
                if q.idiom_id in seen_ids:
                    continue
                new_questions.append(q)
                seen_ids.add(q.idiom_id)
                if len(new_questions) >= remaining:
                    break
        else:
            new_questions = []

    questions = reask_questions + new_questions
    if not questions:
        await update.message.reply_text("No idioms to review right now. Ingest more PDFs!")
        return
    for q in questions:
        await _send_question(chat_id, q, context)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def cmd_story(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = config.today_local().isoformat()
    with db.connect(config.DB_PATH) as conn:
        row = db.get_daily_story(conn, today)

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
            rows = db.due_idioms(conn, config.today_local(), config.DAILY_IDIOM_COUNT, update.effective_user.id)
            story_idioms = [
                {"id": r["id"], "phrase": r["phrase"], "meaning": r["meaning"], "viet": r["vietnamese_equiv"] or ""}
                for r in rows
            ]
        if not story_idioms:
            await update.message.reply_text("No idioms available yet.")
            return
        idiom_ids_str = ",".join(str(i["id"]) for i in story_idioms)
        story = generate_daily_story(story_idioms, client)
        phrases = "\n".join(
            f'• "{i["phrase"]}"' + (f' — {i["viet"]}' if i["viet"] and i["viet"] != "—" else "")
            for i in story_idioms
        )
        if not story:
            await update.message.reply_text("Couldn't generate a story right now, try again.")
            return

    story_vi = translate_to_vietnamese(story, client, idioms=story_idioms)
    with db.connect(config.DB_PATH) as conn:
        db.save_daily_story(conn, today, story, phrases, story_vi, idiom_ids_str)
    vi_section = f"\n\n🇻🇳 Bản dịch:\n{story_vi}" if story_vi else ""
    await update.message.reply_text(f"📖 Today's story\n\n{phrases}\n\n{story}{vi_section}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start  — register\n"
        "/quiz   — get 5 questions now\n"
        "/quiz N — get N questions (max 20)\n"
        "/story  — today's idiom story\n"
        "/stats  — see your progress\n"
        "/skipped — list idioms you've marked as known\n"
        "/unskip <id or phrase> — bring a skipped idiom back\n"
        "/help   — this message"
    )


async def cmd_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 4 or parts[0] != "ans":
        await query.edit_message_reply_markup(reply_markup=None)
        return

    idiom_id = int(parts[1])
    chosen = int(parts[2])
    correct_index = int(parts[3])

    chat_id = query.message.chat_id
    with db.connect(config.DB_PATH) as conn:
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
    stem, kind, options = cached if cached else (None, "forward", None)

    if stem and kind in ("forward", "completion"):
        filled = stem.replace("___", f"[{phrase}]")
        context_line = f"\n\n{filled}"
    else:
        story = idiom["story"] or idiom["example"] or meaning
        context_line = f"\n\n{story}"

    if chosen == correct_index:
        reply = f"✅ Correct!\n\n{phrase} — {meaning}{viet_line}{context_line}"
    else:
        chosen_label = options[chosen] if options and chosen < len(options) else LETTERS[chosen]
        reply = (
            f"❌ You chose: {chosen_label}\n"
            f"✅ Answer: {phrase}\n\n"
            f"{meaning}{viet_line}{context_line}"
        )

    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=reply,
        reply_to_message_id=query.message.message_id,
    )


async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await query.edit_message_reply_markup(reply_markup=None)
    # Also clear from production cache and re-ask queue so it doesn't keep coming back
    with db.connect(config.DB_PATH) as conn:
        db.clear_production_pending(conn, chat_id, query.message.message_id)
        conn.execute(
            "DELETE FROM reask_queue WHERE chat_id = ? AND idiom_id = ?",
            (chat_id, idiom_id),
        )


async def send_daily_quiz(application: Application) -> None:
    from anthropic import Anthropic
    from .examples import generate_daily_story, translate_to_vietnamese

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    today = config.today_local()

    with db.connect(config.DB_PATH) as conn:
        users = db.all_users(conn)

    if not users:
        return

    # Generate a shared daily story using a SMALLER idiom set so the rendered
    # message stays under Telegram's 4096-char per-message limit.
    # Reserve some slots for FRESH phase-0 idioms (never seen in any story)
    # so the introduction pipeline keeps flowing.
    first_uid = users[0]
    fresh_slots = min(5, config.STORY_IDIOM_COUNT // 3)
    remaining_slots = config.STORY_IDIOM_COUNT - fresh_slots
    with db.connect(config.DB_PATH) as conn:
        fresh_rows = db.never_in_story_idioms(conn, fresh_slots, [], first_uid)
        fresh_ids = [r["id"] for r in fresh_rows]
        pipeline_rows = db.build_daily_rows(
            conn, today, remaining_slots, first_uid, extra_exclude_ids=fresh_ids,
        )
    story_rows = fresh_rows + pipeline_rows

    story_idioms = [
        {"id": r["id"], "phrase": r["phrase"], "meaning": r["meaning"], "viet": r["vietnamese_equiv"] or ""}
        for r in story_rows
    ]
    idiom_ids_str = ",".join(str(i["id"]) for i in story_idioms)
    phrases_str = "\n".join(
        f'• "{i["phrase"]}"' + (f' — {i["viet"]}' if i["viet"] and i["viet"] != "—" else "")
        for i in story_idioms
    )

    daily_story = ""
    story_vi = ""
    try:
        daily_story = generate_daily_story(story_idioms, client)
        if daily_story:
            story_vi = translate_to_vietnamese(daily_story, client, idioms=story_idioms)
            with db.connect(config.DB_PATH) as conn:
                db.save_daily_story(conn, today.isoformat(), daily_story, phrases_str, story_vi, idiom_ids_str)
    except Exception as e:
        logger.error("Failed to generate daily story: %s", e)

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
                rows = db.build_daily_rows(
                    conn, today, config.DAILY_IDIOM_COUNT + 10, chat_id,
                    extra_exclude_ids=recent_sent,
                )
                questions = build_questions_from_rows(conn, rows, chat_id)
            questions = questions[:config.DAILY_IDIOM_COUNT]
        except Exception as e:
            logger.error("Daily quiz: build failed for user %s: %s", chat_id, e)
            continue

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
                    text=f"🌟 Idiom of the Day\n\n{iotd_phrase}\n{iotd_meaning}{viet_line}{story_line}",
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
                rows = db.build_daily_rows(
                    conn, today, config.EVENING_IDIOM_COUNT + 10, chat_id,
                    extra_exclude_ids=sent_today,
                )
                questions = build_questions_from_rows(conn, rows, chat_id)
            questions = questions[:config.EVENING_IDIOM_COUNT]
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
                {"id": r["id"], "phrase": r["phrase"], "meaning": r["meaning"], "viet": r["vietnamese_equiv"] or ""}
                for r in rows
            ]
            bullet_lines = []
            for r in rows:
                total_ans = (r["correct"] or 0) + (r["wrong"] or 0)
                pct = round((r["correct"] or 0) / total_ans * 100) if total_ans else 0
                bullet_lines.append(f"• {r['phrase']} ({pct}% correct)")
            bullets = "\n".join(bullet_lines)

            phrases_str = "\n".join(
                f'• "{i["phrase"]}"' + (f' — {i["viet"]}' if i["viet"] and i["viet"] != "—" else "")
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
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )

    answer = resp.content[0].text.strip() if resp.content else "Sorry, I couldn't process that."
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

    stem = (
        f"🔁 Turn {turn_number}/{MULTI_TURN_MAX} — same idiom, new situation.\n\n"
        f"Meaning: {meaning}{viet_line}\n\n"
        f"Situation: {situation}\n\n"
        "Recall the idiom that fits and use it in a sentence.\n\n"
        "Reply to this message with your sentence 👇"
    )
    sent = await bot.send_message(chat_id=chat_id, text=stem)
    new_used = "|".join(used_situations + [situation])
    with db.connect(config.DB_PATH) as conn:
        db.save_production_pending(
            conn, chat_id, sent.message_id, idiom_id, phrase,
            turn_number=turn_number, used_situations=new_used,
        )


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
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
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

    raw = resp.content[0].text.strip() if resp.content else ""

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    result_line = lines[0].upper() if lines else ""
    feedback = "\n".join(lines[1:]) if len(lines) > 1 else raw

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

    icon = "✅" if correct else "❌"
    await update.message.reply_text(f"{icon} {feedback}")

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


async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    # Only handle replies to the bot's own messages
    if not msg.reply_to_message.from_user or msg.reply_to_message.from_user.id != context.bot.id:
        return

    replied_id = msg.reply_to_message.message_id
    chat_id = msg.chat_id

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
                db.clear_production_pending(conn, chat_id, replied_id)
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
        model="claude-sonnet-4-6",
        max_tokens=300,
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

    answer = resp.content[0].text.strip() if resp.content else "Sorry, I couldn't process that."
    await msg.reply_text(answer)


def run(db_path: str) -> None:
    logging.basicConfig(level=logging.INFO)

    scheduler = AsyncIOScheduler(timezone=config.TZ)

    async def on_startup(app: Application) -> None:
        await app.bot.set_my_commands([
            BotCommand("q", "Quick quiz (alias /quiz)"),
            BotCommand("quiz", "Get 5 questions now (/quiz N for more)"),
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
    application.add_handler(CommandHandler(["story", "s"], cmd_story))
    application.add_handler(CommandHandler(["stats", "stat"], cmd_stats))
    application.add_handler(CommandHandler(["help", "h"], cmd_help))
    application.add_handler(CommandHandler("skipped", cmd_skipped))
    application.add_handler(CommandHandler("unskip", cmd_unskip))
    application.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^ans:"))
    application.add_handler(CallbackQueryHandler(handle_skip, pattern=r"^skip:"))
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, handle_user_reply))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.REPLY & ~filters.COMMAND, handle_direct_message))

    application.run_polling(drop_pending_updates=True)
