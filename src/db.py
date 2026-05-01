import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path


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
    vietnamese_equiv TEXT,
    source_pdf       TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP
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
"""


@contextmanager
def connect(db_path: str):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


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
        "SELECT id, phrase, meaning FROM idioms WHERE vietnamese_equiv IS NULL OR vietnamese_equiv = ''"
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


def random_distractor_idioms(conn, exclude_id: int, n: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, phrase FROM idioms WHERE id != ? ORDER BY RANDOM() LIMIT ?",
        (exclude_id, n),
    ))


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
