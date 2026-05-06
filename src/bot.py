import logging
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

# Cache message_id → (stem, kind) so handle_answer can show the filled-in sentence.
# Lost on restart (acceptable — fallback to meaning-only display).
_stem_cache: dict[int, tuple[str, str]] = {}


def _question_text(q: Question) -> str:
    opts = "\n".join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(q.options))
    prefix = "↩️ Try again — you missed this one before.\n\n" if q.reask else ""
    if q.kind == "reverse":
        return f"{prefix}What does '{q.phrase}' mean?\n\n{q.stem}\n\n{opts}"
    if q.kind == "vietnamese":
        return f"{prefix}Which English idiom matches?\n\n{q.stem}\n\n{opts}"
    if q.kind == "completion":
        return f"{prefix}{q.stem}\n\n{opts}"
    return f"{prefix}Fill in the blank:\n\n{q.stem}\n\n{opts}"


def _keyboard(q: Question) -> InlineKeyboardMarkup:
    # Encode correct_index in every button so answers survive bot restarts
    buttons = [
        InlineKeyboardButton(
            LETTERS[i],
            callback_data=f"ans:{q.idiom_id}:{i}:{q.correct_index}"
        )
        for i in range(len(q.options))
    ]
    return InlineKeyboardMarkup([buttons])


async def _send_question(chat_id: int, q: Question, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=_question_text(q),
        reply_markup=_keyboard(q),
    )
    _stem_cache[msg.message_id] = (q.stem, q.kind)
    # Prevent unbounded growth
    if len(_stem_cache) > 2000:
        oldest = next(iter(_stem_cache))
        del _stem_cache[oldest]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    with db.connect(config.DB_PATH) as conn:
        db.register_user(conn, user.id, user.username)
    await update.message.reply_text(
        f"Hi {user.first_name}!\n\n"
        "I'll send you 10 idiom quizzes every day at 6 AM.\n"
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
        # Prepend any pending re-asks
        reask_rows = db.pop_reasks(conn, chat_id, n)
        reask_questions = []
        for r in reask_rows:
            idiom = db.get_idiom(conn, r["idiom_id"])
            if idiom:
                try:
                    q = build_question(conn, idiom)
                except ValueError:
                    try:
                        q = build_reverse_question(conn, idiom)
                    except ValueError:
                        continue
                q.reask = True
                reask_questions.append(q)

        remaining = n - len(reask_questions)
        if remaining > 0:
            rows = db.build_daily_rows(conn, date.today(), remaining)
            new_questions = build_questions_from_rows(conn, rows)
        else:
            new_questions = []

    questions = reask_questions + new_questions
    if not questions:
        await update.message.reply_text("No idioms to review right now. Ingest more PDFs!")
        return
    for q in questions:
        await _send_question(chat_id, q, context)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db.connect(config.DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM idioms").fetchone()[0]
        reviewed = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE repetitions > 0"
        ).fetchone()[0]
        row = conn.execute(
            "SELECT SUM(correct) as c, SUM(wrong) as w FROM reviews"
        ).fetchone()
        correct, wrong = row["c"] or 0, row["w"] or 0
        total_ans = correct + wrong
        accuracy = round(correct / total_ans * 100) if total_ans else 0
        weakest = conn.execute(
            """SELECT i.phrase, r.ease, r.correct, r.wrong
               FROM idioms i JOIN reviews r ON i.id = r.idiom_id
               WHERE r.repetitions > 0
               ORDER BY r.ease ASC LIMIT 5"""
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
    today = date.today().isoformat()
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
            rows = db.due_idioms(conn, date.today(), config.DAILY_IDIOM_COUNT)
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
        "/help   — this message"
    )


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
        db.apply_review(conn, idiom_id, quality)
        if chosen != correct_index:
            db.add_reask(conn, chat_id, idiom_id)

    phrase = idiom["phrase"]
    meaning = idiom["meaning"]
    viet = idiom["vietnamese_equiv"] or ""
    viet_line = f"\n🇻🇳 {viet}" if viet and viet != "—" else ""

    # Retrieve the original question stem so the answer shows the same sentence
    cached = _stem_cache.pop(query.message.message_id, None)
    stem, kind = cached if cached else (None, "forward")

    if stem and kind in ("forward", "completion"):
        # Fill the blank with the correct answer
        filled = stem.replace("___", f"[{phrase}]")
        context_line = f"\n\n{filled}"
    else:
        # For reverse/vietnamese or no cache: show the story as context
        story = idiom["story"] or idiom["example"] or meaning
        context_line = f"\n\n{story}"

    if chosen == correct_index:
        reply = f"✅ Correct!\n\n{phrase} — {meaning}{viet_line}{context_line}"
    else:
        reply = f"❌ Wrong. Answer: {phrase}\n\n{meaning}{viet_line}{context_line}"

    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=reply,
        reply_to_message_id=query.message.message_id,
    )


async def send_daily_quiz(application: Application) -> None:
    from anthropic import Anthropic
    from .examples import generate_daily_story, translate_to_vietnamese

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    today = date.today()

    with db.connect(config.DB_PATH) as conn:
        users = db.all_users(conn)
        iotd = db.weakest_idiom(conn)
        rows = db.build_daily_rows(conn, today, config.DAILY_IDIOM_COUNT)

    if not rows:
        logger.warning("Daily quiz: no questions available.")
        return

    story_idioms = [
        {"id": r["id"], "phrase": r["phrase"], "meaning": r["meaning"], "viet": r["vietnamese_equiv"] or ""}
        for r in rows
    ]
    idiom_ids_str = ",".join(str(i["id"]) for i in story_idioms)
    phrases_str = "\n".join(
        f'• "{i["phrase"]}"' + (f' — {i["viet"]}' if i["viet"] and i["viet"] != "—" else "")
        for i in story_idioms
    )

    # Step 1: generate English story + Vietnamese translation
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

    # Step 2: build quiz questions using stored per-idiom examples (not story sentences)
    with db.connect(config.DB_PATH) as conn:
        questions = build_questions_from_rows(conn, rows)

    if not questions:
        logger.warning("Daily quiz: failed to build questions.")
        return

    for chat_id in users:
        try:
            # Idiom of the Day — weakest idiom as a reading item
            if iotd:
                iotd_phrase = iotd["phrase"]
                iotd_meaning = iotd["meaning"]
                iotd_story = iotd["story"] or iotd["example"] or ""
                viet = iotd["vietnamese_equiv"] or ""
                viet_line = f"\n🇻🇳 {viet}" if viet and viet != "—" else ""
                story_line = f"\n\n{iotd_story}" if iotd_story else ""
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"🌟 Idiom of the Day\n\n{iotd_phrase}\n{iotd_meaning}{viet_line}{story_line}",
                )

            # Daily story — read this, then the quiz will test you on it
            if daily_story:
                vi_section = f"\n\n🇻🇳 Bản dịch:\n{story_vi}" if story_vi else ""
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"📖 Today's story\n\n{phrases_str}\n\n{daily_story}{vi_section}",
                )

            await application.bot.send_message(
                chat_id=chat_id,
                text=f"Good morning! Now let's test your recall 🌅 ({len(questions)} questions)",
            )
            for q in questions:
                await _send_question(chat_id, q, type("ctx", (), {"bot": application.bot})())
        except Exception as e:
            logger.error("Failed to send daily quiz to %s: %s", chat_id, e)


async def send_weekly_review(application: Application) -> None:
    from anthropic import Anthropic
    from .examples import generate_daily_story, translate_to_vietnamese

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    with db.connect(config.DB_PATH) as conn:
        users = db.all_users(conn)
        rows = db.weak_idioms_this_week(conn, 10)

    if not rows:
        return

    story_idioms = [
        {"id": r["id"], "phrase": r["phrase"], "meaning": r["meaning"], "viet": r["vietnamese_equiv"] or ""}
        for r in rows
    ]

    # Build bullet list with accuracy
    bullet_lines = []
    for r in rows:
        total_ans = (r["correct"] or 0) + (r["wrong"] or 0)
        pct = round((r["correct"] or 0) / total_ans * 100) if total_ans else 0
        bullet_lines.append(f"• {r['phrase']} ({pct}% correct)")
    bullets = "\n".join(bullet_lines)

    # Generate story + Vietnamese translation
    daily_story = ""
    story_vi = ""
    phrases_str = "\n".join(
        f'• "{i["phrase"]}"' + (f' — {i["viet"]}' if i["viet"] and i["viet"] != "—" else "")
        for i in story_idioms
    )
    try:
        daily_story = generate_daily_story(story_idioms, client)
        if daily_story:
            story_vi = translate_to_vietnamese(daily_story, client, idioms=story_idioms)
    except Exception as e:
        logger.error("Failed to generate weekly review story: %s", e)

    # Build questions
    with db.connect(config.DB_PATH) as conn:
        questions = build_questions_from_rows(conn, rows)

    for chat_id in users:
        try:
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


async def handle_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    # Only handle replies to the bot's own messages
    if not msg.reply_to_message.from_user or msg.reply_to_message.from_user.id != context.bot.id:
        return

    user_question = msg.text or ""
    bot_context = msg.reply_to_message.text or ""

    await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")

    from anthropic import Anthropic
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=(
            "You are a friendly English idiom learning assistant embedded in a Telegram quiz bot. "
            "The user is learning English idioms. They have replied to a bot message (a quiz question, "
            "answer feedback, or a story) with a follow-up question. "
            "Use the context to give a helpful, concise answer. "
            "Explain meanings, give extra examples, compare similar idioms, or clarify anything they ask. "
            "Keep it conversational and under 200 words."
        ),
        messages=[{
            "role": "user",
            "content": f"Context from the bot:\n{bot_context}\n\nMy question:\n{user_question}",
        }],
    )

    answer = resp.content[0].text.strip() if resp.content else "Sorry, I couldn't process that."
    await msg.reply_text(answer)


def run(db_path: str) -> None:
    logging.basicConfig(level=logging.INFO)

    scheduler = AsyncIOScheduler(timezone=config.TZ)

    async def on_startup(app: Application) -> None:
        scheduler.add_job(
            send_daily_quiz,
            "cron",
            hour=config.DAILY_HOUR,
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
        logger.info("Scheduler started. Daily quiz at %d:00 %s", config.DAILY_HOUR, config.TZ)

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
    application.add_handler(CommandHandler("quiz", cmd_quiz))
    application.add_handler(CommandHandler("story", cmd_story))
    application.add_handler(CommandHandler(["stats", "stat"], cmd_stats))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^ans:"))
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, handle_user_reply))

    application.run_polling(drop_pending_updates=True)
