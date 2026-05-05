import os
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
DAILY_IDIOM_COUNT: int = int(os.getenv("DAILY_IDIOM_COUNT", "15"))
DB_PATH: str = os.getenv("DB_PATH", "data/idioms.db")
TZ: ZoneInfo = ZoneInfo(os.getenv("TZ", "Asia/Ho_Chi_Minh"))
