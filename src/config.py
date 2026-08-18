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
TZ: ZoneInfo = ZoneInfo(os.getenv("TZ", "Asia/Ho_Chi_Minh"))


def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return datetime.now(TZ).date()
