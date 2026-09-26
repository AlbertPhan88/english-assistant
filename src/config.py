import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
DAILY_HOUR: int = int(os.getenv("DAILY_HOUR", "6"))
EVENING_HOUR: int = int(os.getenv("EVENING_HOUR", "19"))
DAILY_IDIOM_COUNT: int = int(os.getenv("DAILY_IDIOM_COUNT", "30"))
EVENING_IDIOM_COUNT: int = int(os.getenv("EVENING_IDIOM_COUNT", "15"))
# Daily story uses a smaller idiom count — Telegram messages are capped at 4096 chars.
STORY_IDIOM_COUNT: int = int(os.getenv("STORY_IDIOM_COUNT", "15"))
DB_PATH: str = os.getenv("DB_PATH", "data/idioms.db")

# --- Models ---
# Three tiers by what the call is for, each overridable from .env.
#   CONTENT  — learner-facing prose: stories, translations, tutor replies,
#              content fixes. Quality is visible in every message.
#   GRADER   — judges production answers. Its verdict drives SM-2 scheduling,
#              so a wrong call costs real review time.
#   BULK     — high-volume batch tagging where the output is a short label.
# Sonnet 5 costs the same per token as Sonnet 4.6 ($3/$15 per 1M), so the
# content tier is a free upgrade. Haiku 4.5 stays the cheapest at $1/$5.
CONTENT_MODEL: str = os.getenv("CONTENT_MODEL", "claude-sonnet-5")
GRADER_MODEL: str = os.getenv("GRADER_MODEL", "claude-sonnet-5")
BULK_MODEL: str = os.getenv("BULK_MODEL", "claude-haiku-4-5")
TZ: ZoneInfo = ZoneInfo(os.getenv("TZ", "Asia/Ho_Chi_Minh"))


def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return datetime.now(TZ).date()


def response_text(resp) -> str:
    """Concatenated text of a Messages response, ignoring non-text blocks.

    Newer models return thinking blocks before the answer, so `content[0]` is
    not reliably the text — reading `.text` off a ThinkingBlock raises. Always
    filter by block type.
    """
    if not getattr(resp, "content", None):
        return ""
    return "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    ).strip()
