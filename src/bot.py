import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from . import config, db
from .quiz import Question, build_daily_set, build_question

logger = logging.getLogger(__name__)

# In-memory map: (chat_id, idiom_id) -> correct_index
_pending: dict[tuple[int, int], int] = {}

LETTERS = ["A", "B", "C", "D"]


def _question_text(q: Question) -> str:
    opts = "\n".join(f"{LETTERS[i]}. {opt}" for i, opt in enumerate(q.options))
    return f"Fill in the blank:\n\n{q.stem}\n\n{opts}"


def _keyboard(q: Question) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(LETTERS[i], callback_data=f"ans:{q.idiom_id}:{i}")
        for i in range(len(q.options))
    ]
    return InlineKeyboardMarkup([buttons])


async def _send_question(chat_id: int, q: Question, context: ContextTypes.DEFAULT_TYPE) -> None:
    _pending[(chat_id, q.idiom_id)] = q.correct_index
    await context.bot.send_message(
        chat_id=chat_id,
        text=_question_text(q),
        reply_markup=_keyboard(q),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    with db.connect(config.DB_PATH) as conn:
        db.register_user(conn, user.id, user.username)
    await update.message.reply_text(
        f"Hi {user.first_name}! 👋\n\n"
        "I'll send you 5 idiom quizzes every day at 6 AM.\n"
        "Type /quiz anytime for an extra one, /stats to see your progress."
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
        questions = build_daily_set(conn, n)
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


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start — register\n"
        "/quiz  — get a question now\n"
        "/stats — see your progress\n"
        "/help  — this message"
    )


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "ans":
        return

    idiom_id = int(parts[1])
    chosen = int(parts[2])
    chat_id = query.message.chat_id
    key = (chat_id, idiom_id)

    correct_index = _pending.pop(key, None)
    if correct_index is None:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    with db.connect(config.DB_PATH) as conn:
        idiom = db.get_idiom(conn, idiom_id)
        quality = 5 if chosen == correct_index else 2
        db.apply_review(conn, idiom_id, quality)

    phrase = idiom["phrase"]
    meaning = idiom["meaning"]
    example = idiom["example"] or meaning
    viet = idiom["vietnamese_equiv"] or ""
    viet_line = f"\n🇻🇳 {viet}" if viet and viet != "—" else ""

    if chosen == correct_index:
        reply = f"✅ Correct!\n\n{phrase} — {meaning}{viet_line}\n\n{example}"
    else:
        reply = f"❌ Wrong. Answer: {phrase}\n\n{meaning}{viet_line}\n\n{example}"

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(reply)


async def send_daily_quiz(application: Application) -> None:
    with db.connect(config.DB_PATH) as conn:
        users = db.all_users(conn)
        questions = build_daily_set(conn, config.DAILY_IDIOM_COUNT)

    if not questions:
        logger.warning("Daily quiz: no questions available.")
        return

    for chat_id in users:
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"Good morning! Here are your {len(questions)} idioms for today 🌅",
            )
            for q in questions:
                await _send_question(chat_id, q, type("ctx", (), {"bot": application.bot})())
        except Exception as e:
            logger.error("Failed to send daily quiz to %s: %s", chat_id, e)


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
    application.add_handler(CommandHandler(["stats", "stat"], cmd_stats))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CallbackQueryHandler(handle_answer, pattern=r"^ans:"))

    application.run_polling(drop_pending_updates=True)
