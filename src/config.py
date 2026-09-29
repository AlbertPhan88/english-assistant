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
# Ceiling on write-a-sentence questions per set. They cost far more effort than a
# multiple-choice tap, so a set that fills up with them is exhausting even when
# the question count is unchanged. 0 disables production entirely; a high value
# restores the old behaviour of letting the rotation decide.
PRODUCTION_PER_SESSION: int = int(os.getenv("PRODUCTION_PER_SESSION", "6"))
EVENING_IDIOM_COUNT: int = int(os.getenv("EVENING_IDIOM_COUNT", "15"))
# Daily story uses a smaller idiom count — Telegram messages are capped at 4096 chars.
STORY_IDIOM_COUNT: int = int(os.getenv("STORY_IDIOM_COUNT", "15"))
DB_PATH: str = os.getenv("DB_PATH", "data/idioms.db")

# --- Access control ---
# ADMIN_CHAT_ID may manage access with /users, /allow and /block, and is told
# when someone new tries to register.
ADMIN_CHAT_ID: int = int(os.getenv("ADMIN_CHAT_ID", "0"))
# With INVITE_ONLY on, a new /start registers the user but leaves them blocked
# until the admin allows them, so a stranger who finds the bot gets nothing.
# Existing users are unaffected — their blocked flag is whatever it already is.
INVITE_ONLY: bool = os.getenv("INVITE_ONLY", "1") not in ("0", "false", "False", "")

# --- Models ---
# Three tiers by what the call is for, each overridable from .env.
#   CONTENT  — learner-facing prose: stories, translations, tutor replies,
#              content fixes. Quality is visible in every message.
#   GRADER   — judges production answers. Its verdict drives SM-2 scheduling,
#              so a wrong call costs real review time.
#   BULK     — high-volume batch tagging where the output is a short label.
# Sonnet 5 costs the same per token as Sonnet 4.6 ($3/$15 per 1M), so the
# content tier is a free upgrade. Haiku 4.5 stays the cheapest at $1/$5.
# Reasoning models spend part of max_tokens on thinking before writing a word,
# so a budget sized for the visible reply alone gets truncated — and a truncated
# reply arrives as an empty text block, not an error. Every call on a reasoning
# tier adds this headroom on top of the room its answer needs.
THINKING_HEADROOM: int = int(os.getenv("THINKING_HEADROOM", "1200"))

CONTENT_MODEL: str = os.getenv("CONTENT_MODEL", "claude-sonnet-5")
GRADER_MODEL: str = os.getenv("GRADER_MODEL", "claude-sonnet-5")
BULK_MODEL: str = os.getenv("BULK_MODEL", "claude-haiku-4-5")
TZ: ZoneInfo = ZoneInfo(os.getenv("TZ", "Asia/Ho_Chi_Minh"))


def now_local() -> datetime:
    return datetime.now(TZ)


def today_local() -> date:
    return datetime.now(TZ).date()


def reply_budget(visible_tokens: int) -> int:
    """max_tokens for a call whose visible answer has a roughly fixed length."""
    return visible_tokens + THINKING_HEADROOM


def rewrite_budget(source: str, multiple: float = 3.0) -> int:
    """max_tokens for a call that rewrites `source` into something as long again.

    Translating or editing a passage produces output proportional to its input,
    and the thinking that precedes it scales the same way, so a fixed headroom
    under-serves a long passage and truncates it. Vietnamese is diacritic-heavy
    and runs well over the usual chars-per-token, so the estimate is deliberately
    generous — these calls run twice a day, not in bulk.
    """
    # max_tokens is a ceiling, not a charge — only generated tokens are billed —
    # so the estimate is deliberately loose. Measured on these stories, thinking
    # alone runs 1000-1300 tokens and spikes higher, independently of length.
    est_tokens = len(source) / 2.0
    return max(8000, int(est_tokens * multiple) + 4 * THINKING_HEADROOM)


def was_truncated(resp) -> bool:
    """True when the model ran out of budget mid-answer."""
    return getattr(resp, "stop_reason", None) == "max_tokens"


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
