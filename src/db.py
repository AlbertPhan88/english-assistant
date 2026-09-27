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
    blocked     INTEGER NOT NULL DEFAULT 0,
    registered  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reask_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    idiom_id   INTEGER NOT NULL REFERENCES idioms(id) ON DELETE CASCADE,
    added_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_stories (
    user_id   INTEGER NOT NULL DEFAULT 0,
    date      TEXT NOT NULL,
    story     TEXT NOT NULL,
    phrases   TEXT NOT NULL,
    story_vi  TEXT,
    idiom_ids TEXT,
    PRIMARY KEY (user_id, date)
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


def _migrate_stories_to_multiuser(conn) -> None:
    """Give daily_stories a user_id, copying each story to every user who was
    registered when it was sent.

    Stories used to be keyed by date alone: one story was generated from the
    first user's pipeline and broadcast to everyone, and the story-introduced
    quiz bucket read the table globally. Fanning the history out per user
    preserves what each existing user actually saw, while a user who joined
    later correctly has no story history at all.
    """
    rows = list(conn.execute(
        "SELECT date, story, phrases, story_vi, idiom_ids FROM daily_stories"
    ))
    users = list(conn.execute("SELECT chat_id, registered FROM users"))
    conn.execute("ALTER TABLE daily_stories RENAME TO daily_stories_old")
    conn.execute(
        """CREATE TABLE daily_stories (
            user_id   INTEGER NOT NULL DEFAULT 0,
            date      TEXT NOT NULL,
            story     TEXT NOT NULL,
            phrases   TEXT NOT NULL,
            story_vi  TEXT,
            idiom_ids TEXT,
            PRIMARY KEY (user_id, date)
        )"""
    )
    for row in rows:
        for user in users:
            # registered is a timestamp; a story dated on or after the day the
            # user joined is one they were sent.
            if row["date"] >= (user["registered"] or "")[:10]:
                conn.execute(
                    """INSERT OR IGNORE INTO daily_stories(
                         user_id, date, story, phrases, story_vi, idiom_ids
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (user["chat_id"], row["date"], row["story"], row["phrases"],
                     row["story_vi"], row["idiom_ids"]),
                )
    conn.execute("DROP TABLE daily_stories_old")


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
    # Every graded production answer, kept so a verdict can be reviewed later.
    # The cache row is deleted on grading, so without this the sentence and the
    # judgement on it are both lost the moment the reply is processed.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS production_answers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            idiom_id    INTEGER NOT NULL,
            phrase      TEXT NOT NULL,
            situation   TEXT NOT NULL DEFAULT '',
            sentence    TEXT NOT NULL,
            correct     INTEGER NOT NULL,
            feedback    TEXT NOT NULL DEFAULT '',
            turn_number INTEGER NOT NULL DEFAULT 1,
            answered_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_production_answers_chat_time "
        "ON production_answers(chat_id, answered_at)"
    )

    # message_id -> idiom_id for every question sent, so a reply can be traced
    # back to its idiom whatever the question type. production_cache only covers
    # production questions, and the in-memory stem cache dies with the process.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS question_msg (
            chat_id    INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            idiom_id   INTEGER NOT NULL,
            kind       TEXT NOT NULL DEFAULT '',
            sent_at    TEXT NOT NULL,
            answered   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, message_id)
        )"""
    )
    qm_cols = {row[1] for row in conn.execute("PRAGMA table_info(question_msg)")}
    if "answered" not in qm_cols:
        conn.execute(
            "ALTER TABLE question_msg ADD COLUMN answered INTEGER NOT NULL DEFAULT 0"
        )

    # Content corrections raised by users, with the before state kept so a bad
    # fix can be traced or undone.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS content_fixes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id     INTEGER NOT NULL,
            idiom_id    INTEGER NOT NULL,
            complaint   TEXT NOT NULL,
            field       TEXT NOT NULL,
            old_value   TEXT,
            new_value   TEXT,
            applied_at  TEXT NOT NULL
        )"""
    )

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
    if "register" not in cols:
        # Tone and register tags, stored as one JSON object. See
        # examples.REGISTER_AXES for the axes and their allowed values.
        conn.execute("ALTER TABLE idioms ADD COLUMN register TEXT")

    u_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "blocked" not in u_cols:
        conn.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")

    ds_cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_stories)")}
    if "story_vi" not in ds_cols:
        conn.execute("ALTER TABLE daily_stories ADD COLUMN story_vi TEXT")
    if "idiom_ids" not in ds_cols:
        conn.execute("ALTER TABLE daily_stories ADD COLUMN idiom_ids TEXT")
    if "user_id" not in ds_cols:
        _migrate_stories_to_multiuser(conn)

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


def register_user(conn, chat_id: int, username: str | None,
                  blocked: bool = False) -> None:
    """Register a user, seeding a review row per idiom.

    `blocked` applies to genuinely new users only; an existing user's state is
    never changed by a repeat /start.
    """
    conn.execute(
        "INSERT OR IGNORE INTO users(chat_id, username, blocked) VALUES (?, ?, ?)",
        (chat_id, username, 1 if blocked else 0),
    )
    # Create review rows for all existing idioms this user doesn't have yet
    conn.execute(
        "INSERT OR IGNORE INTO reviews(user_id, idiom_id, boot_phase) "
        "SELECT ?, id, 0 FROM idioms",
        (chat_id,),
    )


def all_users(conn) -> list[int]:
    """Chat ids that scheduled sends should reach — blocked users excluded."""
    return [
        row["chat_id"]
        for row in conn.execute("SELECT chat_id FROM users WHERE blocked = 0")
    ]


def is_blocked(conn, chat_id: int) -> bool:
    """True when the user exists and is blocked.

    An unknown chat_id is not blocked: /start gates new registrations itself.
    """
    row = conn.execute(
        "SELECT blocked FROM users WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return bool(row and row["blocked"])


def set_blocked(conn, chat_id: int, blocked: bool) -> bool:
    cur = conn.execute(
        "UPDATE users SET blocked = ? WHERE chat_id = ?",
        (1 if blocked else 0, chat_id),
    )
    return cur.rowcount > 0


def list_users(conn) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT chat_id, username, registered, blocked FROM users ORDER BY registered"
    ))


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


def save_daily_story(conn, user_id: int, date_str: str, story: str, phrases: str,
                     story_vi: str = "", idiom_ids: str = "") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO daily_stories(
             user_id, date, story, phrases, story_vi, idiom_ids
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, date_str, story, phrases, story_vi, idiom_ids),
    )


def get_daily_story(conn, user_id: int, date_str: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT story, phrases, story_vi, idiom_ids FROM daily_stories "
        "WHERE user_id = ? AND date = ?",
        (user_id, date_str),
    ).fetchone()


def _story_idiom_ids(conn, user_id: int) -> set[int]:
    """Every idiom this user has met in one of their own daily stories."""
    ids: set[int] = set()
    for row in conn.execute(
        "SELECT idiom_ids FROM daily_stories WHERE user_id = ? AND idiom_ids != ''",
        (user_id,),
    ):
        for tok in row[0].split(","):
            tok = tok.strip()
            if tok.isdigit():
                ids.add(int(tok))
    return ids


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


def log_production_answer(conn, chat_id: int, idiom_id: int, phrase: str,
                          situation: str, sentence: str, correct: bool,
                          feedback: str, turn_number: int) -> None:
    """Record a graded production answer, so the verdict can be checked later."""
    from . import config
    conn.execute(
        """INSERT INTO production_answers(
             chat_id, idiom_id, phrase, situation, sentence, correct,
             feedback, turn_number, answered_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (chat_id, idiom_id, phrase, situation, sentence, 1 if correct else 0,
         feedback, turn_number, config.now_local().isoformat()),
    )


def recent_production_answers(conn, chat_id: int, n: int = 10,
                              only_wrong: bool = False) -> list[sqlite3.Row]:
    """Most recent graded production answers, newest first."""
    where = "WHERE chat_id = ?" + (" AND correct = 0" if only_wrong else "")
    return list(conn.execute(
        f"""SELECT phrase, situation, sentence, correct, feedback,
                   turn_number, answered_at
            FROM production_answers {where}
            ORDER BY answered_at DESC LIMIT ?""",
        (chat_id, n),
    ))


def clear_production_pending_upto(conn, chat_id: int, idiom_id: int,
                                  message_id: int) -> None:
    """Clear the answered pending row and every older one for the same idiom.

    An idiom sent on several days and left unanswered each time leaves a row per
    send, so clearing only the replied-to message would keep the idiom in the
    backlog forever. Rows newer than the reply are kept: by this point a
    multi-turn follow-up may already have queued one.
    """
    conn.execute(
        "DELETE FROM production_cache "
        "WHERE chat_id = ? AND idiom_id = ? AND message_id <= ?",
        (chat_id, idiom_id, message_id),
    )


def unanswered_production_idioms(conn, chat_id: int, n: int) -> list[sqlite3.Row]:
    """Idioms whose production questions were sent but never graded, freshest first.

    A production_cache row lives until the answer is graded, so the table doubles
    as the backlog. Deduped by idiom, and idioms marked known are left out.

    Ordered by the most recent send rather than the first. A question from
    yesterday still has its story and quiz context in mind, where one from two
    months ago has to be re-learnt before it can be answered. Chronic repeats
    are not buried by this: an idiom the daily sets keep re-sending has a recent
    last send too, so it still surfaces early.
    """
    return list(conn.execute(
        """SELECT p.idiom_id, MIN(p.created_at) AS first_sent,
                  MAX(p.created_at) AS last_sent, COUNT(*) AS times
           FROM production_cache p
           JOIN reviews r ON r.idiom_id = p.idiom_id AND r.user_id = p.chat_id
           WHERE p.chat_id = ? AND r.skipped = 0
           GROUP BY p.idiom_id
           ORDER BY last_sent DESC
           LIMIT ?""",
        (chat_id, n),
    ))


def count_unanswered_production(conn, chat_id: int) -> int:
    return conn.execute(
        """SELECT COUNT(DISTINCT p.idiom_id) FROM production_cache p
           JOIN reviews r ON r.idiom_id = p.idiom_id AND r.user_id = p.chat_id
           WHERE p.chat_id = ? AND r.skipped = 0""",
        (chat_id,),
    ).fetchone()[0]


def log_question_msg(conn, chat_id: int, message_id: int, idiom_id: int,
                     kind: str) -> None:
    from . import config
    conn.execute(
        """INSERT OR REPLACE INTO question_msg(
             chat_id, message_id, idiom_id, kind, sent_at
           ) VALUES (?, ?, ?, ?, ?)""",
        (chat_id, message_id, idiom_id, kind, config.now_local().isoformat()),
    )


def get_question_msg(conn, chat_id: int, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT idiom_id, kind FROM question_msg "
        "WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()


def claim_question(conn, chat_id: int, message_id: int) -> bool:
    """Claim a question as answered. True for the first caller only.

    Telegram delivers a callback per tap, so a double tap on an answer button
    arrives as two updates. Grading both would apply the SM-2 transition twice.
    The UPDATE is the lock: SQLite serializes it, so exactly one caller sees a
    row change.
    """
    cur = conn.execute(
        "UPDATE question_msg SET answered = 1 "
        "WHERE chat_id = ? AND message_id = ? AND answered = 0",
        (chat_id, message_id),
    )
    return cur.rowcount > 0


def mark_question_answered(conn, chat_id: int, message_id: int) -> None:
    """Close a question without gating on the claim.

    Production answers arrive as replies and are deliberately retryable — a
    transient grader failure tells the user to reply again — so that path must
    not claim up front. Once a grade lands, though, the question is settled and
    must not be re-gradable by pressing "Don't know" on the same message.
    """
    conn.execute(
        "UPDATE question_msg SET answered = 1 WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    )


def prune_question_msg(conn, keep_days: int = 30) -> None:
    """Drop message anchors older than `keep_days`; replies never arrive that late."""
    from datetime import timedelta
    from . import config
    cutoff = (config.today_local() - timedelta(days=keep_days)).isoformat()
    conn.execute("DELETE FROM question_msg WHERE sent_at < ?", (cutoff,))


def log_content_fix(conn, chat_id: int, idiom_id: int, complaint: str,
                    field: str, old_value: str | None, new_value: str) -> None:
    from . import config
    conn.execute(
        """INSERT INTO content_fixes(
             chat_id, idiom_id, complaint, field, old_value, new_value, applied_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (chat_id, idiom_id, complaint, field, old_value, new_value,
         config.now_local().isoformat()),
    )


def apply_content_fix(conn, idiom_id: int, field: str, value: str) -> None:
    """Write one corrected field. `field` must already be whitelisted by the caller."""
    conn.execute(f"UPDATE idioms SET {field} = ? WHERE id = ?", (value, idiom_id))


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


def get_review_row(conn, idiom_id: int, user_id: int) -> sqlite3.Row | None:
    """One idiom joined to its review state, shaped like the daily-set rows.

    Question builders dispatch on boot_phase and next_kind, so a caller holding
    only an idiom id needs this to rebuild the question type the idiom is due for.
    """
    return conn.execute(
        """SELECT i.*, r.ease, r.interval, r.repetitions, r.due_date, r.last_seen,
                  r.correct, r.wrong, r.boot_phase, r.next_kind
           FROM idioms i JOIN reviews r ON i.id = r.idiom_id
           WHERE i.id = ? AND r.user_id = ?""",
        (idiom_id, user_id),
    ).fetchone()


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


# Index into quiz._KIND_BUILDERS for a production question. Slots 1 and 3 of the
# SM-2 rotation are both production; 1 is the earlier of the two.
PRODUCTION_KIND = 1


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
        if new_phase == 3 and quality < 3:
            # Missed the production stage. Graduate anyway, but hand SM-2 the
            # miss so the ease drops, and queue the retry as another production
            # question a day out. Repeating it later the same day would only
            # test working memory; rotating on to a multiple-choice question
            # would never re-test producing the phrase at all.
            ease, interval, reps = sm2(
                row["ease"], row["interval"], row["repetitions"], quality
            )
            retry_due = (config.today_local() + timedelta(days=interval)).isoformat()
            conn.execute(
                """UPDATE reviews SET boot_phase=3, ease=?, interval=?, repetitions=?,
                   due_date=?, last_seen=?, correct=correct+?, wrong=wrong+?,
                   next_kind=?
                   WHERE user_id=? AND idiom_id=?""",
                (ease, interval, reps, retry_due, now, correct_delta, wrong_delta,
                 PRODUCTION_KIND, user_id, idiom_id),
            )
        elif new_phase == 3:
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
        # A miss holds the question type instead of rotating on: you retry the
        # skill you just failed, spaced by whatever interval SM-2 hands back.
        current_kind = (row["next_kind"] or 0) % 5
        next_kind = current_kind if quality < 3 else (current_kind + 1) % 5
        conn.execute(
            """UPDATE reviews SET ease=?, interval=?, repetitions=?, due_date=?, last_seen=?,
               correct=correct+?, wrong=wrong+?, next_kind=?
               WHERE user_id=? AND idiom_id=?""",
            (ease, interval, reps, due, now, correct_delta, wrong_delta, next_kind, user_id, idiom_id),
        )


# --- Theme tagging ---

def idioms_missing_register(conn) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, phrase, meaning FROM idioms "
        "WHERE register IS NULL OR register = ''"
    ))


def update_register(conn, idiom_id: int, register_json: str) -> None:
    conn.execute(
        "UPDATE idioms SET register = ? WHERE id = ?", (register_json, idiom_id)
    )


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
    story_id_set = _story_idiom_ids(conn, user_id)
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
    story_id_set = _story_idiom_ids(conn, user_id)
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
