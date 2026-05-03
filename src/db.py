import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path


THEME_ORDER = [
    "communication", "relationships", "emotions", "work", "success", "money", "time",
    "conflict", "deception", "knowledge", "body", "animals", "food", "nature", "luck", "general",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested_pdfs (
    filename    TEXT PRIMARY KEY,
    ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS idioms (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase           TEXT NOT NULL UNIQUE,
    meaning          TEXT NOT NULL,
    example          TEXT,
    story            TEXT,
    vietnamese_equiv TEXT,
    source_pdf       TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    theme            TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    idiom_id    INTEGER PRIMARY KEY REFERENCES idioms(id) ON DELETE CASCADE,
    ease        REAL NOT NULL DEFAULT 2.5,
    interval    INTEGER NOT NULL DEFAULT 0,
    repetitions INTEGER NOT NULL DEFAULT 0,
    due_date    TEXT NOT NULL DEFAULT (date('now')),
    last_seen   TEXT,
    correct     INTEGER NOT NULL DEFAULT 0,
    wrong       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    chat_id     INTEGER PRIMARY KEY,
    username    TEXT,
    registered  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reask_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    idiom_id   INTEGER NOT NULL REFERENCES idioms(id) ON DELETE CASCADE,
    added_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_stories (
    date      TEXT PRIMARY KEY,
    story     TEXT NOT NULL,
    phrases   TEXT NOT NULL,
    story_vi  TEXT,
    idiom_ids TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def connect(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(idioms)")}
    if "story" not in cols:
        conn.execute("ALTER TABLE idioms ADD COLUMN story TEXT")
    if "theme" not in cols:
        conn.execute("ALTER TABLE idioms ADD COLUMN theme TEXT")

    ds_cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_stories)")}
    if "story_vi" not in ds_cols:
        conn.execute("ALTER TABLE daily_stories ADD COLUMN story_vi TEXT")
    if "idiom_ids" not in ds_cols:
        conn.execute("ALTER TABLE daily_stories ADD COLUMN idiom_ids TEXT")

    # Create app_settings table if it doesn't exist yet
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)"
    )


def init(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def add_idiom(conn, phrase: str, meaning: str, example: str | None, source_pdf: str | None) -> int | None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO idioms(phrase, meaning, example, source_pdf) VALUES (?, ?, ?, ?)",
        (phrase.strip(), meaning.strip(), example, source_pdf),
    )
    if cur.rowcount == 0:
        return None
    idiom_id = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO reviews(idiom_id) VALUES (?)", (idiom_id,))
    return idiom_id


def is_pdf_ingested(conn, filename: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM ingested_pdfs WHERE filename = ?", (filename,)
    ).fetchone() is not None


def mark_pdf_ingested(conn, filename: str) -> None:
    conn.execute("INSERT OR IGNORE INTO ingested_pdfs(filename) VALUES (?)", (filename,))


def update_example(conn, idiom_id: int, example: str) -> None:
    conn.execute("UPDATE idioms SET example = ? WHERE id = ?", (example, idiom_id))


def update_vietnamese(conn, idiom_id: int, viet: str) -> None:
    conn.execute("UPDATE idioms SET vietnamese_equiv = ? WHERE id = ?", (viet, idiom_id))


def idioms_missing_vietnamese(conn) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, phrase, meaning FROM idioms WHERE vietnamese_equiv IS NULL OR vietnamese_equiv = '' OR vietnamese_equiv = '—'"
    ))


def register_user(conn, chat_id: int, username: str | None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO users(chat_id, username) VALUES (?, ?)",
        (chat_id, username),
    )


def all_users(conn) -> list[int]:
    return [row["chat_id"] for row in conn.execute("SELECT chat_id FROM users")]


def idioms_missing_example(conn) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, phrase, meaning FROM idioms WHERE example IS NULL OR example = ''"
    ))


def update_story(conn, idiom_id: int, story: str) -> None:
    conn.execute("UPDATE idioms SET story = ? WHERE id = ?", (story, idiom_id))


def idioms_missing_story(conn) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, phrase, meaning FROM idioms WHERE story IS NULL OR story = ''"
    ))


def random_distractor_idioms(conn, exclude_id: int, n: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, phrase FROM idioms WHERE id != ? ORDER BY RANDOM() LIMIT ?",
        (exclude_id, n),
    ))


def random_distractor_meanings(conn, exclude_id: int, n: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, meaning FROM idioms WHERE id != ? ORDER BY RANDOM() LIMIT ?",
        (exclude_id, n),
    ))


def add_reask(conn, chat_id: int, idiom_id: int) -> None:
    conn.execute(
        "INSERT INTO reask_queue(chat_id, idiom_id) VALUES (?, ?)",
        (chat_id, idiom_id),
    )


def pop_reasks(conn, chat_id: int, n: int) -> list[sqlite3.Row]:
    rows = list(conn.execute(
        "SELECT id, idiom_id FROM reask_queue WHERE chat_id = ? ORDER BY added_at ASC LIMIT ?",
        (chat_id, n),
    ))
    if rows:
        ids = [r["id"] for r in rows]
        conn.execute(f"DELETE FROM reask_queue WHERE id IN ({','.join('?'*len(ids))})", ids)
    return rows


def save_daily_story(conn, date_str: str, story: str, phrases: str, story_vi: str = "", idiom_ids: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO daily_stories(date, story, phrases, story_vi, idiom_ids) VALUES (?, ?, ?, ?, ?)",
        (date_str, story, phrases, story_vi, idiom_ids),
    )


def get_daily_story(conn, date_str: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT story, phrases, story_vi, idiom_ids FROM daily_stories WHERE date = ?", (date_str,)
    ).fetchone()


def get_idioms_by_ids(conn, idiom_ids_str: str) -> list[dict]:
    """Return idiom dicts (phrase, meaning, viet) for the given comma-separated IDs."""
    if not idiom_ids_str:
        return []
    ids = [int(i) for i in idiom_ids_str.split(",") if i.strip()]
    placeholders = ",".join("?" * len(ids))
    rows = list(conn.execute(
        f"SELECT id, phrase, meaning, vietnamese_equiv FROM idioms WHERE id IN ({placeholders})", ids
    ))
    order = {i: pos for pos, i in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["id"], 0))
    return [{"id": r["id"], "phrase": r["phrase"], "meaning": r["meaning"], "viet": r["vietnamese_equiv"] or ""} for r in rows]


def build_phrases_str(conn, idiom_ids_str: str) -> str:
    """Rebuild the idiom bullet list from current DB data (picks up latest Vietnamese)."""
    if not idiom_ids_str:
        return ""
    ids = [int(i) for i in idiom_ids_str.split(",") if i.strip()]
    placeholders = ",".join("?" * len(ids))
    rows = list(conn.execute(
        f"SELECT id, phrase, vietnamese_equiv FROM idioms WHERE id IN ({placeholders})", ids
    ))
    order = {i: pos for pos, i in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["id"], 0))
    return "\n".join(
        f'• "{r["phrase"]}"' + (f' — {r["vietnamese_equiv"]}' if r["vietnamese_equiv"] and r["vietnamese_equiv"] != "—" else "")
        for r in rows
    )


def weakest_idiom(conn) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT i.* FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.repetitions > 0
           ORDER BY r.ease ASC, r.wrong DESC LIMIT 1"""
    ).fetchone()


def get_idiom(conn, idiom_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM idioms WHERE id = ?", (idiom_id,)).fetchone()


def due_idioms(conn, today: date, limit: int) -> list[sqlite3.Row]:
    today_str = today.isoformat()
    rows = list(conn.execute(
        """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.due_date <= ?
           ORDER BY r.due_date ASC, r.ease ASC, RANDOM()
           LIMIT ?""",
        (today_str, limit),
    ))
    if len(rows) < limit:
        existing_ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(existing_ids)) if existing_ids else "NULL"
        extra = list(conn.execute(
            f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
                FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE i.id NOT IN ({placeholders}) AND r.due_date > ?
                ORDER BY r.last_seen ASC NULLS FIRST
                LIMIT ?""",
            (*existing_ids, today_str, limit - len(rows)),
        ))
        rows.extend(extra)
    return rows


def apply_review(conn, idiom_id: int, quality: int) -> None:
    from .scheduler import sm2
    row = conn.execute(
        "SELECT ease, interval, repetitions FROM reviews WHERE idiom_id = ?", (idiom_id,)
    ).fetchone()
    if not row:
        return
    ease, interval, reps = sm2(row["ease"], row["interval"], row["repetitions"], quality)
    from datetime import date, timedelta
    due = (date.today() + timedelta(days=interval)).isoformat()
    now = datetime.utcnow().isoformat()
    correct_delta = 1 if quality >= 3 else 0
    wrong_delta = 0 if quality >= 3 else 1
    conn.execute(
        """UPDATE reviews SET ease=?, interval=?, repetitions=?, due_date=?, last_seen=?,
           correct=correct+?, wrong=wrong+? WHERE idiom_id=?""",
        (ease, interval, reps, due, now, correct_delta, wrong_delta, idiom_id),
    )


# --- Theme tagging ---

def idioms_missing_theme(conn) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, phrase, meaning FROM idioms WHERE theme IS NULL OR theme = ''"
    ))


def update_theme(conn, idiom_id: int, theme: str) -> None:
    conn.execute("UPDATE idioms SET theme = ? WHERE id = ?", (theme, idiom_id))


# --- App settings ---

def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_settings(key, value) VALUES (?, ?)", (key, value)
    )


# --- Clustered daily set helpers ---

def warm_up_idioms(conn, n: int, exclude_ids: list[int]) -> list[sqlite3.Row]:
    """Idioms with wrong > 0 AND last_seen >= 7 days ago, ordered by last_seen DESC."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        return list(conn.execute(
            f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
                FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE r.wrong > 0 AND r.last_seen <= ?
                AND i.id NOT IN ({placeholders})
                ORDER BY r.last_seen DESC
                LIMIT ?""",
            (cutoff, *exclude_ids, n),
        ))
    return list(conn.execute(
        """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.wrong > 0 AND r.last_seen <= ?
           ORDER BY r.last_seen DESC
           LIMIT ?""",
        (cutoff, n),
    ))


def new_idioms_from_theme(conn, theme: str, n: int, exclude_ids: list[int]) -> list[sqlite3.Row]:
    """New (repetitions=0) idioms with the given theme, ordered by id ASC."""
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        return list(conn.execute(
            f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
                FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE r.repetitions = 0 AND i.theme = ?
                AND i.id NOT IN ({placeholders})
                ORDER BY i.id ASC
                LIMIT ?""",
            (theme, *exclude_ids, n),
        ))
    return list(conn.execute(
        """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.repetitions = 0 AND i.theme = ?
           ORDER BY i.id ASC
           LIMIT ?""",
        (theme, n),
    ))


def advance_theme_if_exhausted(conn) -> None:
    """If current theme has 0 unseen idioms, walk THEME_ORDER to find next with unseen."""
    current = get_setting(conn, "current_theme", THEME_ORDER[0])
    unseen_count = conn.execute(
        "SELECT COUNT(*) FROM idioms i JOIN reviews r ON i.id = r.idiom_id "
        "WHERE r.repetitions = 0 AND i.theme = ?",
        (current,),
    ).fetchone()[0]
    if unseen_count > 0:
        return
    # Find next theme with unseen idioms
    try:
        start_idx = THEME_ORDER.index(current)
    except ValueError:
        start_idx = 0
    for offset in range(1, len(THEME_ORDER) + 1):
        candidate = THEME_ORDER[(start_idx + offset) % len(THEME_ORDER)]
        count = conn.execute(
            "SELECT COUNT(*) FROM idioms i JOIN reviews r ON i.id = r.idiom_id "
            "WHERE r.repetitions = 0 AND i.theme = ?",
            (candidate,),
        ).fetchone()[0]
        if count > 0:
            set_setting(conn, "current_theme", candidate)
            return
    # All themes exhausted — leave setting as-is


def build_daily_rows(conn, today: date, total: int = 10) -> list[sqlite3.Row]:
    """Build a clustered daily set: warm-up + SM-2 review + new from theme."""
    today_str = today.isoformat()

    # Check if there are enough wrong answers for warm-up
    total_wrong = conn.execute("SELECT SUM(wrong) FROM reviews").fetchone()[0] or 0

    # Bucket 1: warm-up (3 slots) — skip if total wrong < 3
    warmup_target = 3
    warmup: list[sqlite3.Row] = []
    if total_wrong >= 3:
        warmup = warm_up_idioms(conn, warmup_target, [])

    warmup_ids = [r["id"] for r in warmup]

    # Bucket 2: SM-2 review (3 slots)
    review_target = 3
    if warmup_ids:
        placeholders = ",".join("?" * len(warmup_ids))
        review = list(conn.execute(
            f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
                FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE r.repetitions > 0 AND r.due_date <= ?
                AND i.id NOT IN ({placeholders})
                ORDER BY r.due_date ASC, r.ease ASC
                LIMIT ?""",
            (today_str, *warmup_ids, review_target),
        ))
    else:
        review = list(conn.execute(
            """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
               FROM idioms i JOIN reviews r ON i.id = r.idiom_id
               WHERE r.repetitions > 0 AND r.due_date <= ?
               ORDER BY r.due_date ASC, r.ease ASC
               LIMIT ?""",
            (today_str, review_target),
        ))

    review_ids = [r["id"] for r in review]
    exclude_ids = warmup_ids + review_ids

    # Bucket 3: new from current theme (4 slots)
    new_target = 4
    advance_theme_if_exhausted(conn)
    current_theme = get_setting(conn, "current_theme", THEME_ORDER[0])
    new_rows = new_idioms_from_theme(conn, current_theme, new_target, exclude_ids)

    all_rows = warmup + review + new_rows
    obtained = len(all_rows)

    # If any bucket came up short, expand others to fill total
    if obtained < total:
        shortfall = total - obtained
        all_ids = [r["id"] for r in all_rows]
        if all_ids:
            placeholders = ",".join("?" * len(all_ids))
            extra = list(conn.execute(
                f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
                    FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                    WHERE i.id NOT IN ({placeholders})
                    ORDER BY r.due_date ASC, r.ease ASC, RANDOM()
                    LIMIT ?""",
                (*all_ids, shortfall),
            ))
        else:
            extra = list(conn.execute(
                """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
                   FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                   ORDER BY r.due_date ASC, r.ease ASC, RANDOM()
                   LIMIT ?""",
                (shortfall,),
            ))
        all_rows.extend(extra)

    return all_rows[:total]


# --- Weekly review ---

def weak_idioms_this_week(conn, n: int) -> list[sqlite3.Row]:
    """Idioms with last_seen >= 7 days ago, ordered by wrong/(correct+wrong+1) DESC."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    return list(conn.execute(
        """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.last_seen >= ?
           ORDER BY CAST(r.wrong AS REAL) / (r.correct + r.wrong + 1) DESC
           LIMIT ?""",
        (cutoff, n),
    ))
