import sqlite3
from contextlib import contextmanager
from datetime import date
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
    user_id          INTEGER NOT NULL,
    idiom_id         INTEGER NOT NULL REFERENCES idioms(id) ON DELETE CASCADE,
    ease             REAL NOT NULL DEFAULT 2.5,
    interval         INTEGER NOT NULL DEFAULT 0,
    repetitions      INTEGER NOT NULL DEFAULT 0,
    due_date         TEXT NOT NULL DEFAULT (date('now')),
    last_seen        TEXT,
    correct          INTEGER NOT NULL DEFAULT 0,
    wrong            INTEGER NOT NULL DEFAULT 0,
    boot_phase       INTEGER NOT NULL DEFAULT 0,
    next_kind        INTEGER NOT NULL DEFAULT 0,
    next_example_idx INTEGER NOT NULL DEFAULT 0,
    next_story_idx   INTEGER NOT NULL DEFAULT 0,
    last_iotd_at     TEXT,
    skipped          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, idiom_id)
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
    user_id INTEGER NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS idiom_examples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    idiom_id  INTEGER NOT NULL REFERENCES idioms(id) ON DELETE CASCADE,
    sentence  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idiom_stories (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    idiom_id  INTEGER NOT NULL REFERENCES idioms(id) ON DELETE CASCADE,
    story     TEXT NOT NULL
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


def _migrate_reviews_to_multiuser(conn, old_cols: set) -> None:
    first_user = conn.execute("SELECT chat_id FROM users ORDER BY registered LIMIT 1").fetchone()
    owner_id = first_user["chat_id"] if first_user else 0

    def col_or(name: str, default: int) -> str:
        return name if name in old_cols else str(default)

    boot_expr = (
        "boot_phase" if "boot_phase" in old_cols
        else "CASE WHEN repetitions > 0 THEN 3 ELSE 0 END"
    )

    conn.execute("ALTER TABLE reviews RENAME TO reviews_old")
    conn.execute("""CREATE TABLE reviews (
        user_id          INTEGER NOT NULL,
        idiom_id         INTEGER NOT NULL REFERENCES idioms(id) ON DELETE CASCADE,
        ease             REAL NOT NULL DEFAULT 2.5,
        interval         INTEGER NOT NULL DEFAULT 0,
        repetitions      INTEGER NOT NULL DEFAULT 0,
        due_date         TEXT NOT NULL DEFAULT (date('now')),
        last_seen        TEXT,
        correct          INTEGER NOT NULL DEFAULT 0,
        wrong            INTEGER NOT NULL DEFAULT 0,
        boot_phase       INTEGER NOT NULL DEFAULT 0,
        next_kind        INTEGER NOT NULL DEFAULT 0,
        next_example_idx INTEGER NOT NULL DEFAULT 0,
        next_story_idx   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, idiom_id)
    )""")
    conn.execute(
        f"""INSERT INTO reviews(
                user_id, idiom_id, ease, interval, repetitions, due_date,
                last_seen, correct, wrong, boot_phase, next_kind,
                next_example_idx, next_story_idx)
            SELECT ?, idiom_id, ease, interval, repetitions, due_date,
                last_seen, correct, wrong,
                {boot_expr},
                {col_or('next_kind', 0)},
                {col_or('next_example_idx', 0)},
                {col_or('next_story_idx', 0)}
            FROM reviews_old""",
        (owner_id,),
    )
    conn.execute("DROP TABLE reviews_old")


def _migrate_settings_to_multiuser(conn) -> None:
    first_user = conn.execute("SELECT chat_id FROM users ORDER BY registered LIMIT 1").fetchone()
    owner_id = first_user["chat_id"] if first_user else 0

    conn.execute("ALTER TABLE app_settings RENAME TO app_settings_old")
    conn.execute("""CREATE TABLE app_settings (
        user_id INTEGER NOT NULL,
        key     TEXT NOT NULL,
        value   TEXT,
        PRIMARY KEY (user_id, key)
    )""")
    conn.execute(
        "INSERT INTO app_settings(user_id, key, value) SELECT ?, key, value FROM app_settings_old",
        (owner_id,),
    )
    conn.execute("DROP TABLE app_settings_old")


def _migrate(conn) -> None:
    # Migrate reviews to composite (user_id, idiom_id) PK if needed
    rev_info = conn.execute("PRAGMA table_info(reviews)").fetchall()
    if rev_info:
        rev_cols = {row[1] for row in rev_info}
        if "user_id" not in rev_cols:
            _migrate_reviews_to_multiuser(conn, rev_cols)

    # Migrate app_settings to composite (user_id, key) PK if needed
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    settings_cols = {row[1] for row in conn.execute("PRAGMA table_info(app_settings)")}
    if "user_id" not in settings_cols:
        _migrate_settings_to_multiuser(conn)

    # Backfill review rows for any registered user missing them
    conn.execute(
        "INSERT OR IGNORE INTO reviews(user_id, idiom_id, boot_phase) "
        "SELECT u.chat_id, i.id, 0 FROM users u CROSS JOIN idioms i"
    )

    rev_cols2 = {row[1] for row in conn.execute("PRAGMA table_info(reviews)")}
    if "last_iotd_at" not in rev_cols2:
        conn.execute("ALTER TABLE reviews ADD COLUMN last_iotd_at TEXT")
    if "skipped" not in rev_cols2:
        conn.execute("ALTER TABLE reviews ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0")

    # Persistent production-question cache so user replies always find the target idiom.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS production_cache (
            chat_id    INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            idiom_id   INTEGER NOT NULL,
            phrase     TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            turn_number     INTEGER NOT NULL DEFAULT 1,
            used_situations TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (chat_id, message_id)
        )"""
    )
    pc_cols = {row[1] for row in conn.execute("PRAGMA table_info(production_cache)")}
    if "turn_number" not in pc_cols:
        conn.execute("ALTER TABLE production_cache ADD COLUMN turn_number INTEGER NOT NULL DEFAULT 1")
    if "used_situations" not in pc_cols:
        conn.execute("ALTER TABLE production_cache ADD COLUMN used_situations TEXT NOT NULL DEFAULT ''")
    # Per-day log of sent questions so evening quiz can avoid morning duplicates.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS question_sent (
            chat_id    INTEGER NOT NULL,
            idiom_id   INTEGER NOT NULL,
            sent_date  TEXT NOT NULL,
            PRIMARY KEY (chat_id, idiom_id, sent_date)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_question_sent_chat_date "
        "ON question_sent(chat_id, sent_date)"
    )

    # Add missing columns to idioms
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

    # Create and seed idiom_examples / idiom_stories pools (once)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS idiom_examples "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "idiom_id INTEGER NOT NULL REFERENCES idioms(id) ON DELETE CASCADE, "
        "sentence TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS idiom_stories "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "idiom_id INTEGER NOT NULL REFERENCES idioms(id) ON DELETE CASCADE, "
        "story TEXT NOT NULL)"
    )
    if conn.execute("SELECT COUNT(*) FROM idiom_examples").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO idiom_examples(idiom_id, sentence) "
            "SELECT id, example FROM idioms WHERE example IS NOT NULL AND example != ''"
        )
    if conn.execute("SELECT COUNT(*) FROM idiom_stories").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO idiom_stories(idiom_id, story) "
            "SELECT id, story FROM idioms WHERE story IS NOT NULL AND story != ''"
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
    # Create a review row for every registered user
    conn.execute(
        "INSERT OR IGNORE INTO reviews(user_id, idiom_id, boot_phase) "
        "SELECT chat_id, ?, 0 FROM users",
        (idiom_id,),
    )
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
        "SELECT id, phrase, meaning FROM idioms "
        "WHERE vietnamese_equiv IS NULL OR vietnamese_equiv = '' OR vietnamese_equiv = '—'"
    ))


def register_user(conn, chat_id: int, username: str | None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO users(chat_id, username) VALUES (?, ?)",
        (chat_id, username),
    )
    # Create review rows for all existing idioms this user doesn't have yet
    conn.execute(
        "INSERT OR IGNORE INTO reviews(user_id, idiom_id, boot_phase) "
        "SELECT ?, id, 0 FROM idioms",
        (chat_id,),
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


def get_next_example(conn, idiom_id: int, user_id: int) -> str | None:
    idx_row = conn.execute(
        "SELECT next_example_idx FROM reviews WHERE user_id = ? AND idiom_id = ?",
        (user_id, idiom_id),
    ).fetchone()
    idx = idx_row["next_example_idx"] if idx_row else 0
    rows = conn.execute(
        "SELECT sentence FROM idiom_examples WHERE idiom_id = ? ORDER BY id ASC", (idiom_id,)
    ).fetchall()
    if not rows:
        return None
    sentence = rows[idx % len(rows)]["sentence"]
    conn.execute(
        "UPDATE reviews SET next_example_idx = next_example_idx + 1 "
        "WHERE user_id = ? AND idiom_id = ?",
        (user_id, idiom_id),
    )
    return sentence


def get_next_story(conn, idiom_id: int, user_id: int) -> str | None:
    idx_row = conn.execute(
        "SELECT next_story_idx FROM reviews WHERE user_id = ? AND idiom_id = ?",
        (user_id, idiom_id),
    ).fetchone()
    idx = idx_row["next_story_idx"] if idx_row else 0
    rows = conn.execute(
        "SELECT story FROM idiom_stories WHERE idiom_id = ? ORDER BY id ASC", (idiom_id,)
    ).fetchall()
    if not rows:
        return None
    story = rows[idx % len(rows)]["story"]
    conn.execute(
        "UPDATE reviews SET next_story_idx = next_story_idx + 1 "
        "WHERE user_id = ? AND idiom_id = ?",
        (user_id, idiom_id),
    )
    return story


def add_example(conn, idiom_id: int, sentence: str) -> None:
    conn.execute("INSERT INTO idiom_examples(idiom_id, sentence) VALUES (?, ?)", (idiom_id, sentence))


def add_idiom_story(conn, idiom_id: int, story: str) -> None:
    conn.execute("INSERT INTO idiom_stories(idiom_id, story) VALUES (?, ?)", (idiom_id, story))


def idioms_needing_examples(conn, target: int = 5) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT i.id, i.phrase, i.meaning, COUNT(e.id) AS example_count
           FROM idioms i LEFT JOIN idiom_examples e ON i.id = e.idiom_id
           GROUP BY i.id HAVING COUNT(e.id) < ?
           ORDER BY COUNT(e.id) ASC, i.id ASC""",
        (target,),
    ))


def idioms_needing_stories(conn, target: int = 3) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT i.id, i.phrase, i.meaning, COUNT(s.id) AS story_count
           FROM idioms i LEFT JOIN idiom_stories s ON i.id = s.idiom_id
           GROUP BY i.id HAVING COUNT(s.id) < ?
           ORDER BY COUNT(s.id) ASC, i.id ASC""",
        (target,),
    ))


def random_distractor_last_words(conn, exclude_id: int, n: int) -> list[str]:
    rows = conn.execute(
        "SELECT phrase FROM idioms WHERE id != ? ORDER BY RANDOM() LIMIT ?",
        (exclude_id, n * 3),
    ).fetchall()
    seen: set[str] = set()
    result: list[str] = []
    for row in rows:
        last = row["phrase"].strip().split()[-1].lower()
        if last not in seen:
            seen.add(last)
            result.append(last)
        if len(result) == n:
            break
    return result


def boot_camp_idioms(conn, n: int, user_id: int,
                     exclude_ids: list[int] | None = None) -> list[sqlite3.Row]:
    """Follow-up boot camp idioms split evenly between phase 1 (reverse)
    and phase 2 (production), filling unused quota from the other phase."""
    half = n // 2
    seed_excludes = list(exclude_ids or [])
    cols = (
        "i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, "
        "r.correct, r.wrong, r.boot_phase, r.next_kind"
    )

    def _pick(phase: int, limit: int, exclude_ids: list[int]) -> list[sqlite3.Row]:
        if limit <= 0:
            return []
        if exclude_ids:
            placeholders = ",".join("?" * len(exclude_ids))
            return list(conn.execute(
                f"""SELECT {cols} FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                    WHERE r.user_id = ? AND r.boot_phase = ? AND r.skipped = 0
                    AND i.id NOT IN ({placeholders})
                    ORDER BY i.id ASC LIMIT ?""",
                (user_id, phase, *exclude_ids, limit),
            ))
        return list(conn.execute(
            f"""SELECT {cols} FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE r.user_id = ? AND r.boot_phase = ? AND r.skipped = 0
                ORDER BY i.id ASC LIMIT ?""",
            (user_id, phase, limit),
        ))

    phase2 = _pick(2, half, seed_excludes)
    phase1 = _pick(1, n - len(phase2), seed_excludes + [r["id"] for r in phase2])
    # Backfill any phase-2 shortfall with extra phase-1, and vice versa
    remaining = n - len(phase1) - len(phase2)
    if remaining > 0:
        extra = _pick(2, remaining, seed_excludes + [r["id"] for r in phase2])
        phase2.extend(extra)
    return phase2 + phase1


def add_reask(conn, chat_id: int, idiom_id: int) -> None:
    conn.execute(
        "INSERT INTO reask_queue(chat_id, idiom_id) VALUES (?, ?)",
        (chat_id, idiom_id),
    )


def pop_reasks(conn, chat_id: int, n: int) -> list[sqlite3.Row]:
    """Pop up to n re-ask entries, deduped by idiom_id. All queued entries for
    each popped idiom are removed (multiple wrong attempts collapse to one re-ask).
    """
    rows = list(conn.execute(
        """SELECT MIN(id) AS id, idiom_id, MIN(added_at) AS added_at
           FROM reask_queue WHERE chat_id = ?
           GROUP BY idiom_id
           ORDER BY added_at ASC LIMIT ?""",
        (chat_id, n),
    ))
    if rows:
        idiom_ids = [r["idiom_id"] for r in rows]
        placeholders = ",".join("?" * len(idiom_ids))
        conn.execute(
            f"DELETE FROM reask_queue WHERE chat_id = ? AND idiom_id IN ({placeholders})",
            (chat_id, *idiom_ids),
        )
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


def weakest_idiom(conn, user_id: int) -> sqlite3.Row | None:
    """Pick the user's currently weakest idiom for Idiom of the Day.

    Ranks by lifetime wrong-rate (with a min sample size), prefers items
    seen recently, and skips anything that was Idiom of the Day in the
    past 7 days. Falls back to looser filters when nothing matches.
    """
    base = """
        SELECT i.* FROM idioms i JOIN reviews r ON i.id = r.idiom_id
        WHERE r.user_id = ?
          AND r.skipped = 0
          AND (r.last_iotd_at IS NULL OR r.last_iotd_at < date('now', '-7 days'))
          AND (r.correct + r.wrong) > 0
          {extra}
        ORDER BY (CAST(r.wrong AS REAL) / (r.correct + r.wrong)) DESC,
                 r.wrong DESC,
                 r.last_seen DESC
        LIMIT 1
    """
    filters = [
        "AND (r.correct + r.wrong) >= 2 AND r.last_seen >= date('now', '-30 days')",
        "AND (r.correct + r.wrong) >= 2",
        "",
    ]
    for extra in filters:
        row = conn.execute(base.format(extra=extra), (user_id,)).fetchone()
        if row:
            return row
    return None


def mark_idiom_of_the_day(conn, user_id: int, idiom_id: int, day: date) -> None:
    conn.execute(
        "UPDATE reviews SET last_iotd_at = ? WHERE user_id = ? AND idiom_id = ?",
        (day.isoformat(), user_id, idiom_id),
    )


def mark_skipped(conn, user_id: int, idiom_id: int) -> bool:
    """Mark an idiom as known/skipped for a user. Returns True if a row was updated."""
    cur = conn.execute(
        "UPDATE reviews SET skipped = 1 WHERE user_id = ? AND idiom_id = ?",
        (user_id, idiom_id),
    )
    return cur.rowcount > 0


def unmark_skipped(conn, user_id: int, idiom_id: int) -> bool:
    """Clear the skipped flag. Returns True if a row was updated."""
    cur = conn.execute(
        "UPDATE reviews SET skipped = 0 WHERE user_id = ? AND idiom_id = ?",
        (user_id, idiom_id),
    )
    return cur.rowcount > 0


def list_skipped(conn, user_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT i.id, i.phrase, i.vietnamese_equiv
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.user_id = ? AND r.skipped = 1
           ORDER BY i.phrase""",
        (user_id,),
    ))


def find_idiom_by_phrase(conn, phrase: str) -> sqlite3.Row | None:
    """Case-insensitive exact-phrase lookup."""
    return conn.execute(
        "SELECT * FROM idioms WHERE LOWER(phrase) = LOWER(?)", (phrase.strip(),)
    ).fetchone()


def save_production_pending(conn, chat_id: int, message_id: int, idiom_id: int, phrase: str,
                            turn_number: int = 1, used_situations: str = "") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO production_cache(
             chat_id, message_id, idiom_id, phrase, turn_number, used_situations
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (chat_id, message_id, idiom_id, phrase, turn_number, used_situations),
    )


def get_production_pending(conn, chat_id: int, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT idiom_id, phrase, turn_number, used_situations FROM production_cache "
        "WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()


def clear_production_pending(conn, chat_id: int, message_id: int) -> None:
    conn.execute(
        "DELETE FROM production_cache WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )


def log_question_sent(conn, chat_id: int, idiom_id: int, sent_date: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO question_sent(chat_id, idiom_id, sent_date) VALUES (?, ?, ?)",
        (chat_id, idiom_id, sent_date),
    )


def get_sent_today(conn, chat_id: int, day: str) -> list[int]:
    return [
        r[0] for r in conn.execute(
            "SELECT idiom_id FROM question_sent WHERE chat_id = ? AND sent_date = ?",
            (chat_id, day),
        )
    ]


def get_idiom(conn, idiom_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM idioms WHERE id = ?", (idiom_id,)).fetchone()


def due_idioms(conn, today: date, limit: int, user_id: int) -> list[sqlite3.Row]:
    today_str = today.isoformat()
    rows = list(conn.execute(
        """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.user_id = ? AND r.due_date <= ? AND r.skipped = 0
           ORDER BY r.due_date ASC, r.ease ASC, RANDOM()
           LIMIT ?""",
        (user_id, today_str, limit),
    ))
    if len(rows) < limit:
        existing_ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(existing_ids)) if existing_ids else "NULL"
        extra = list(conn.execute(
            f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen, r.correct, r.wrong
                FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE r.user_id = ? AND i.id NOT IN ({placeholders}) AND r.due_date > ? AND r.skipped = 0
                ORDER BY r.last_seen ASC NULLS FIRST
                LIMIT ?""",
            (user_id, *existing_ids, today_str, limit - len(rows)),
        ))
        rows.extend(extra)
    return rows


def apply_review(conn, idiom_id: int, quality: int, user_id: int) -> None:
    from .scheduler import sm2
    row = conn.execute(
        "SELECT ease, interval, repetitions, boot_phase, next_kind "
        "FROM reviews WHERE user_id = ? AND idiom_id = ?",
        (user_id, idiom_id),
    ).fetchone()
    if not row:
        return

    from . import config
    now = config.now_local().isoformat()
    correct_delta = 1 if quality >= 3 else 0
    wrong_delta = 0 if quality >= 3 else 1
    boot_phase = row["boot_phase"] if row["boot_phase"] is not None else -1

    if 0 <= boot_phase <= 2:
        from datetime import timedelta
        new_phase = boot_phase + 1
        # No same-day delay during boot — let one quiz session advance phase
        # 0 → 1 → 2 → 3 within the same day. SM-2 takes over once graduated.
        due = config.today_local().isoformat()
        if new_phase == 3:
            conn.execute(
                """UPDATE reviews SET boot_phase=3, interval=1, repetitions=1, due_date=?,
                   last_seen=?, correct=correct+?, wrong=wrong+?
                   WHERE user_id=? AND idiom_id=?""",
                (due, now, correct_delta, wrong_delta, user_id, idiom_id),
            )
        else:
            conn.execute(
                """UPDATE reviews SET boot_phase=?, due_date=?, last_seen=?,
                   correct=correct+?, wrong=wrong+? WHERE user_id=? AND idiom_id=?""",
                (new_phase, due, now, correct_delta, wrong_delta, user_id, idiom_id),
            )
    else:
        from datetime import timedelta
        ease, interval, reps = sm2(row["ease"], row["interval"], row["repetitions"], quality)
        due = (config.today_local() + timedelta(days=interval)).isoformat()
        next_kind = ((row["next_kind"] or 0) + 1) % 5
        conn.execute(
            """UPDATE reviews SET ease=?, interval=?, repetitions=?, due_date=?, last_seen=?,
               correct=correct+?, wrong=wrong+?, next_kind=?
               WHERE user_id=? AND idiom_id=?""",
            (ease, interval, reps, due, now, correct_delta, wrong_delta, next_kind, user_id, idiom_id),
        )


# --- Theme tagging ---

def idioms_missing_theme(conn) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, phrase, meaning FROM idioms WHERE theme IS NULL OR theme = ''"
    ))


def update_theme(conn, idiom_id: int, theme: str) -> None:
    conn.execute("UPDATE idioms SET theme = ? WHERE id = ?", (theme, idiom_id))


# --- Per-user app settings ---

def get_setting(conn, key: str, default: str = "", user_id: int = 0) -> str:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE user_id = ? AND key = ?", (user_id, key)
    ).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str, user_id: int = 0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_settings(user_id, key, value) VALUES (?, ?, ?)",
        (user_id, key, value),
    )


# --- Clustered daily set helpers ---

def warm_up_idioms(conn, n: int, exclude_ids: list[int], user_id: int) -> list[sqlite3.Row]:
    """Idioms with wrong > 0 AND last_seen >= 7 days ago for a specific user."""
    from datetime import timedelta
    from . import config
    cutoff = (config.today_local() - timedelta(days=7)).isoformat()
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        return list(conn.execute(
            f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                       r.correct, r.wrong, r.boot_phase, r.next_kind
                FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE r.user_id = ? AND r.wrong > 0 AND r.last_seen <= ? AND r.skipped = 0
                AND i.id NOT IN ({placeholders})
                ORDER BY r.last_seen DESC
                LIMIT ?""",
            (user_id, cutoff, *exclude_ids, n),
        ))
    return list(conn.execute(
        """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                  r.correct, r.wrong, r.boot_phase, r.next_kind
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.user_id = ? AND r.wrong > 0 AND r.last_seen <= ? AND r.skipped = 0
           ORDER BY r.last_seen DESC
           LIMIT ?""",
        (user_id, cutoff, n),
    ))


def new_idioms_from_theme(conn, theme: str, n: int, exclude_ids: list[int], user_id: int) -> list[sqlite3.Row]:
    """New (boot_phase=0) idioms with the given theme for a specific user."""
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        return list(conn.execute(
            f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                       r.correct, r.wrong, r.boot_phase, r.next_kind
                FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE r.user_id = ? AND r.boot_phase = 0 AND i.theme = ? AND r.skipped = 0
                AND i.id NOT IN ({placeholders})
                ORDER BY i.id ASC
                LIMIT ?""",
            (user_id, theme, *exclude_ids, n),
        ))
    return list(conn.execute(
        """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                  r.correct, r.wrong, r.boot_phase, r.next_kind
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.user_id = ? AND r.boot_phase = 0 AND i.theme = ? AND r.skipped = 0
           ORDER BY i.id ASC
           LIMIT ?""",
        (user_id, theme, n),
    ))


def never_in_story_idioms(conn, n: int, exclude_ids: list[int], user_id: int) -> list[sqlite3.Row]:
    """Phase-0 idioms that have NEVER appeared in any daily story. Used to seed
    fresh material into the daily story so the introduction pipeline keeps flowing.
    Prefers idioms with a Vietnamese translation so the story bullet list is useful."""
    story_id_set: set[int] = set()
    for row in conn.execute("SELECT idiom_ids FROM daily_stories WHERE idiom_ids != ''"):
        for tok in row[0].split(","):
            tok = tok.strip()
            if tok:
                try:
                    story_id_set.add(int(tok))
                except ValueError:
                    pass
    excluded = set(exclude_ids) | story_id_set
    ex_placeholders = ",".join("?" * len(excluded)) if excluded else "NULL"
    return list(conn.execute(
        f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                   r.correct, r.wrong, r.boot_phase, r.next_kind
            FROM idioms i JOIN reviews r ON i.id = r.idiom_id
            WHERE r.user_id = ? AND r.boot_phase = 0 AND r.skipped = 0
            AND i.id NOT IN ({ex_placeholders})
            AND i.vietnamese_equiv IS NOT NULL AND i.vietnamese_equiv != '' AND i.vietnamese_equiv != '—'
            ORDER BY RANDOM()
            LIMIT ?""",
        (user_id, *excluded, n),
    ))


def story_introduced_idioms(conn, n: int, exclude_ids: list[int], user_id: int) -> list[sqlite3.Row]:
    """Phase-0 idioms that appeared in ANY daily story — so the user has been
    exposed to the phrase + Vietnamese at least once. Even months-old exposure
    counts: re-encountering an old story idiom is a recall opportunity.
    These feed back into the boot pipeline as forward (MC) quizzes."""
    # Collect distinct idiom_ids from daily_stories.idiom_ids (comma-separated)
    story_id_set: set[int] = set()
    for row in conn.execute("SELECT idiom_ids FROM daily_stories WHERE idiom_ids != ''"):
        for tok in row[0].split(","):
            tok = tok.strip()
            if tok:
                try:
                    story_id_set.add(int(tok))
                except ValueError:
                    pass
    if not story_id_set:
        return []
    story_ids = sorted(story_id_set)
    story_placeholders = ",".join("?" * len(story_ids))
    if exclude_ids:
        ex_placeholders = ",".join("?" * len(exclude_ids))
        return list(conn.execute(
            f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                       r.correct, r.wrong, r.boot_phase, r.next_kind
                FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE r.user_id = ? AND r.boot_phase = 0 AND r.skipped = 0
                AND i.id IN ({story_placeholders})
                AND i.id NOT IN ({ex_placeholders})
                ORDER BY RANDOM()
                LIMIT ?""",
            (user_id, *story_ids, *exclude_ids, n),
        ))
    return list(conn.execute(
        f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                   r.correct, r.wrong, r.boot_phase, r.next_kind
            FROM idioms i JOIN reviews r ON i.id = r.idiom_id
            WHERE r.user_id = ? AND r.boot_phase = 0 AND r.skipped = 0
            AND i.id IN ({story_placeholders})
            ORDER BY RANDOM()
            LIMIT ?""",
        (user_id, *story_ids, n),
    ))


def advance_theme_if_exhausted(conn, user_id: int) -> None:
    """If the user's current theme has no unseen idioms, advance to the next theme."""
    current = get_setting(conn, "current_theme", THEME_ORDER[0], user_id=user_id)
    unseen_count = conn.execute(
        "SELECT COUNT(*) FROM idioms i JOIN reviews r ON i.id = r.idiom_id "
        "WHERE r.user_id = ? AND r.boot_phase = 0 AND i.theme = ? AND r.skipped = 0",
        (user_id, current),
    ).fetchone()[0]
    if unseen_count > 0:
        return
    try:
        start_idx = THEME_ORDER.index(current)
    except ValueError:
        start_idx = 0
    for offset in range(1, len(THEME_ORDER) + 1):
        candidate = THEME_ORDER[(start_idx + offset) % len(THEME_ORDER)]
        count = conn.execute(
            "SELECT COUNT(*) FROM idioms i JOIN reviews r ON i.id = r.idiom_id "
            "WHERE r.user_id = ? AND r.boot_phase = 0 AND i.theme = ?",
            (user_id, candidate),
        ).fetchone()[0]
        if count > 0:
            set_setting(conn, "current_theme", candidate, user_id=user_id)
            return


def build_daily_rows(conn, today: date, total: int = 15, user_id: int = 0,
                     extra_exclude_ids: list[int] | None = None) -> list[sqlite3.Row]:
    """Build a clustered daily set for a specific user.

    `extra_exclude_ids` (optional): idiom ids to skip on top of the internal
    bucket exclusions. Used by the evening quiz to avoid idioms already
    exercised in the morning session.
    """
    today_str = today.isoformat()
    seed_excludes = list(extra_exclude_ids or [])

    # Bucket sizes: sized to intake ~5 fresh/day and ~19 items due/day.
    # boot 6 = 5 intake + 20% miss buffer (3 phase-1 + 3 phase-2)
    # review 8 = absorb graduated pool's actual due-today volume
    # shortfall shrinks accordingly.

    # Bucket 1: boot camp follow-ups (phases 1 and 2)
    boot_rows = boot_camp_idioms(conn, 6, user_id, exclude_ids=seed_excludes)
    boot_ids = [r["id"] for r in boot_rows]

    # Bucket 2: warm-up — recent wrong, not seen in 7+ days
    total_wrong = conn.execute(
        "SELECT SUM(wrong) FROM reviews WHERE user_id = ?", (user_id,)
    ).fetchone()[0] or 0
    warmup: list[sqlite3.Row] = []
    if total_wrong >= 3:
        warmup = warm_up_idioms(conn, 3, boot_ids + seed_excludes, user_id)
    warmup_ids = [r["id"] for r in warmup]

    # Bucket 3: SM-2 review — due today, graduated idioms only
    exclude_ids = boot_ids + warmup_ids + seed_excludes
    review_target = 8
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        review = list(conn.execute(
            f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                       r.correct, r.wrong, r.boot_phase, r.next_kind
                FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                WHERE r.user_id = ? AND r.boot_phase >= 3 AND r.due_date <= ? AND r.skipped = 0
                AND i.id NOT IN ({placeholders})
                ORDER BY r.due_date ASC, r.ease ASC
                LIMIT ?""",
            (user_id, today_str, *exclude_ids, review_target),
        ))
    else:
        review = list(conn.execute(
            """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                      r.correct, r.wrong, r.boot_phase, r.next_kind
               FROM idioms i JOIN reviews r ON i.id = r.idiom_id
               WHERE r.user_id = ? AND r.boot_phase >= 3 AND r.due_date <= ? AND r.skipped = 0
               ORDER BY r.due_date ASC, r.ease ASC
               LIMIT ?""",
            (user_id, today_str, review_target),
        ))
    review_ids = [r["id"] for r in review]

    # Bucket 4: story-introduced phase-0 idioms. Only includes idioms the user
    # has already seen in a daily story (so the phrase + Vietnamese was shown).
    # Refills the boot pipeline so production questions keep flowing.
    exclude_ids = boot_ids + warmup_ids + review_ids + seed_excludes
    new_rows = story_introduced_idioms(conn, 5, exclude_ids, user_id)

    all_rows = boot_rows + warmup + review + new_rows
    obtained = len(all_rows)

    # Fill shortfall from any remaining idioms
    if obtained < total:
        shortfall = total - obtained
        all_ids = list({*(r["id"] for r in all_rows), *seed_excludes})
        if all_ids:
            placeholders = ",".join("?" * len(all_ids))
            extra = list(conn.execute(
                f"""SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                           r.correct, r.wrong, r.boot_phase, r.next_kind
                    FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                    WHERE r.user_id = ? AND r.skipped = 0 AND r.boot_phase > 0
                    AND i.id NOT IN ({placeholders})
                    ORDER BY r.due_date ASC, r.ease ASC, RANDOM()
                    LIMIT ?""",
                (user_id, *all_ids, shortfall),
            ))
        else:
            extra = list(conn.execute(
                """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                          r.correct, r.wrong, r.boot_phase, r.next_kind
                   FROM idioms i JOIN reviews r ON i.id = r.idiom_id
                   WHERE r.user_id = ? AND r.skipped = 0 AND r.boot_phase > 0
                   ORDER BY r.due_date ASC, r.ease ASC, RANDOM()
                   LIMIT ?""",
                (user_id, shortfall),
            ))
        all_rows.extend(extra)

    return all_rows[:total]


# --- Weekly review ---

def weak_idioms_this_week(conn, n: int, user_id: int) -> list[sqlite3.Row]:
    """Idioms reviewed in the past 7 days, ordered by error rate, for a specific user."""
    from datetime import timedelta
    from . import config
    cutoff = (config.today_local() - timedelta(days=7)).isoformat()
    return list(conn.execute(
        """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                  r.correct, r.wrong, r.boot_phase, r.next_kind
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE r.user_id = ? AND r.last_seen >= ? AND r.skipped = 0
           ORDER BY CAST(r.wrong AS REAL) / (r.correct + r.wrong + 1) DESC
           LIMIT ?""",
        (user_id, cutoff, n),
    ))
