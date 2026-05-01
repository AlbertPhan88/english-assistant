# English Idiom Assistant — Implementation Plan

A self-contained spec to hand to an implementing model. Read top-to-bottom, then build module-by-module in the order in §10.

---

## 1. Goal

A personal English-idiom tutor that:

1. Ingests PDF slide decks from Google Drive and extracts idioms (phrase, meaning, optional example).
2. Generates a **funny example sentence** for each idiom using Claude (the user remembers absurd sentences better).
3. Sends a **daily 6:00 AM Telegram quiz** (5 multiple-choice questions, fill-in-the-blank style).
4. Lets the user request more quizzes during the day via `/quiz`.
5. Tracks recall with **SM-2 spaced repetition** so weaker idioms surface more often.

---

## 2. Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| PDF parsing | `pdfplumber` |
| LLM | Anthropic SDK (`anthropic`) — `claude-sonnet-4-6` for extraction, `claude-haiku-4-5-20251001` for funny examples |
| Telegram | `python-telegram-bot` v21 (async) |
| Storage | SQLite (single file at `data/idioms.db`) |
| Scheduler | `APScheduler` (in-process, AsyncIOScheduler) |
| Config | `python-dotenv` reading `.env` |
| Google Drive | Manual download for v1 — drop PDFs into `pdfs/`. (See §11 for Drive MCP integration as v2.) |

---

## 3. Repo layout

```
english-assistant/
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
├── PLAN.md                 ← this file
├── pdfs/                   ← drop PDFs here (gitignored)
├── data/                   ← SQLite db (gitignored)
├── src/
│   ├── __init__.py
│   ├── config.py           ← loads .env, exposes constants
│   ├── db.py               ← schema + helpers (already drafted)
│   ├── extractor.py        ← PDF → idioms (already drafted)
│   ├── examples.py         ← funny example generation (already drafted)
│   ├── scheduler.py        ← SM-2 + due-idiom selection
│   ├── quiz.py             ← build multiple-choice questions
│   ├── bot.py              ← Telegram handlers + daily job
│   └── main.py             ← CLI entrypoints (ingest / run-bot / fill-examples)
└── tests/
    ├── test_scheduler.py
    └── test_quiz.py
```

The first three `src/` files (`db.py`, `extractor.py`, `examples.py`) already exist on disk — verify and reuse. The rest must be written.

---

## 4. Configuration (`.env`)

```
TELEGRAM_BOT_TOKEN=<from @BotFather>
ANTHROPIC_API_KEY=<sk-ant-...>
DAILY_HOUR=6
DAILY_IDIOM_COUNT=5
DB_PATH=data/idioms.db
TZ=Asia/Ho_Chi_Minh        # set to user's local TZ
```

`src/config.py` should:
- Call `load_dotenv()`.
- Expose typed constants (`TELEGRAM_BOT_TOKEN: str`, `DAILY_HOUR: int`, etc.).
- Raise on startup if any required value is missing.

---

## 5. Database schema (already in `src/db.py`)

Three tables:

- **idioms** — `id`, `phrase` (UNIQUE), `meaning`, `example`, `source_pdf`, `created_at`
- **reviews** (1-to-1 with idioms) — `idiom_id` (PK/FK), `ease` (default 2.5), `interval` (days, default 0), `repetitions`, `due_date`, `last_seen`, `correct`, `wrong`
- **users** — `chat_id` (PK), `username`, `registered`

Helper functions already provided:
- `connect(db_path)` — context manager
- `init(db_path)` — runs schema
- `add_idiom(conn, phrase, meaning, example, source_pdf) -> id|None`
- `register_user(conn, chat_id, username)`
- `all_users(conn) -> list[int]`
- `idioms_missing_example(conn)`
- `random_distractor_idioms(conn, exclude_id, n)`

**Add** these helpers to `db.py`:
- `due_idioms(conn, today: date, limit: int) -> list[Row]` — pick idioms where `due_date <= today` ORDER BY `due_date ASC, ease ASC, RANDOM() LIMIT limit`. If fewer than `limit` are due, top up with idioms ordered by `last_seen ASC NULLS FIRST`.
- `apply_review(conn, idiom_id, quality: int)` — runs SM-2 (§6) and writes back `ease`, `interval`, `repetitions`, `due_date`, `last_seen`, `correct`, `wrong`.
- `get_idiom(conn, idiom_id) -> Row`

---

## 6. SM-2 spaced repetition (`src/scheduler.py`)

Implement standard SM-2 with a **binary quality signal** (correct = 5, wrong = 2), since multiple choice doesn't give a 0–5 scale:

```python
def sm2(ease: float, interval: int, repetitions: int, quality: int) -> tuple[float, int, int]:
    # quality: 0..5; <3 = forgot
    if quality < 3:
        repetitions = 0
        interval = 1
    else:
        if repetitions == 0:
            interval = 1
        elif repetitions == 1:
            interval = 6
        else:
            interval = round(interval * ease)
        repetitions += 1
    ease = max(1.3, ease + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    return ease, interval, repetitions
```

`apply_review` then sets `due_date = today + interval days`, increments `correct` or `wrong`, and updates `last_seen = now`.

**Selection rule for daily 5 (§5 `due_idioms`):** prefer due, break ties by lowest ease (= weakest), then random. This satisfies the user's "less remembered idioms with higher frequency" requirement.

---

## 7. Quiz format (`src/quiz.py`)

User wanted **multiple choice with example sentences as options**. Concretely:

**Question type: fill-in-the-blank**

> Pick the idiom that fits this sentence:
>
> *"When my mother-in-law ate the last slice of pizza, I had to ___ to keep the peace."*
>
> A. bite my tongue
> B. spill the beans
> C. break the ice
> D. hit the sack

- **Stem:** the idiom's funny example with the idiom phrase replaced by `___`.
- **Correct option:** the real idiom phrase.
- **Distractors:** 3 random other idioms from the DB (`random_distractor_idioms`).
- **Shuffle** options; remember which letter is correct.

`quiz.py` API:
```python
@dataclass
class Question:
    idiom_id: int
    stem: str
    options: list[str]      # length 4
    correct_index: int

def build_question(conn, idiom_row) -> Question: ...
def build_daily_set(conn, n: int) -> list[Question]: ...
```

Edge case: if DB has < 4 idioms total, `build_question` should raise — bot should send a friendly "ingest more PDFs first" message.

---

## 8. Telegram bot (`src/bot.py`)

Use `python-telegram-bot` v21 async API.

### Commands

| Command | Behavior |
|---|---|
| `/start` | Register `chat_id` in `users`. Reply: greeting + "I'll send 5 idioms every day at 6 AM. Type `/quiz` anytime for more." |
| `/quiz` | Send the next due question (one at a time). |
| `/stats` | Show: total idioms, idioms reviewed, accuracy %, top 5 weakest (lowest ease). |
| `/help` | Show available commands. |

### Question delivery

Each question is one Telegram message with an **InlineKeyboardMarkup** of 4 buttons (A/B/C/D). Callback data: `f"ans:{idiom_id}:{chosen_index}"`.

On callback:
1. Compare `chosen_index` to the stored correct answer (cache in memory dict keyed by `(chat_id, idiom_id)` — keyed by `idiom_id` is fine since one user).
2. If correct → reply ✅ with the original example (un-blanked), bold the idiom, append meaning. `apply_review(quality=5)`.
3. If wrong → reply ❌ with the correct idiom + example + meaning. `apply_review(quality=2)`.
4. Edit the original message to remove buttons (prevent re-answering).

### Daily job

Use `AsyncIOScheduler` in the same process as the bot:
```python
scheduler.add_job(
    send_daily_quiz, "cron",
    hour=DAILY_HOUR, minute=0, timezone=TZ,
)
```
`send_daily_quiz` iterates `all_users(conn)`, builds `DAILY_IDIOM_COUNT` questions, and posts them sequentially with a brief greeting.

### State

Pending-question correctness can live in a process-local dict `pending: dict[tuple[int,int], int]` (chat_id, idiom_id → correct_index). Lost on restart — acceptable.

---

## 9. Entrypoints (`src/main.py`)

A small CLI using `argparse`:

```
python -m src.main init                  # create DB
python -m src.main ingest pdfs/foo.pdf   # extract idioms from a PDF
python -m src.main fill-examples         # generate funny examples for any idiom missing one
python -m src.main run                   # start the Telegram bot + scheduler
python -m src.main stats                 # print DB stats to stdout
```

`ingest` and `fill-examples` use functions already drafted in `extractor.py` / `examples.py`.

---

## 10. Implementation order

Each step is independently testable.

1. **`config.py`** — env loading, fail-fast validation.
2. **Verify `db.py`** runs `init` and creates the schema. Add `due_idioms`, `apply_review`, `get_idiom`.
3. **`scheduler.py`** — `sm2()` pure function + unit tests in `tests/test_scheduler.py` (test the canonical SM-2 sequence: q=5 three times → intervals 1, 6, ~15).
4. **`quiz.py`** — `build_question` + `build_daily_set`. Unit-test option shuffling and correct-index tracking with a fake conn (or use a tmp SQLite).
5. **`extractor.py`** — already drafted. Smoke-test on one PDF: should populate `idioms` rows.
6. **`examples.py`** — already drafted. Run `fill-examples` and spot-check 3 outputs for the idiom appearing verbatim.
7. **`bot.py`** — implement handlers, then the daily job. Test manually: `/start`, `/quiz`, answer right and wrong, check DB updates.
8. **`main.py`** — wire up the CLI subcommands.
9. **README** — setup steps (see §13).

---

## 11. Google Drive integration (deferred to v2)

For v1, user manually downloads PDFs from Drive into `pdfs/`. v2 wiring:

- The Claude.ai Drive MCP exposes search/list/get tools after OAuth.
- Add `src/drive.py` with a `sync_folder(folder_id)` function that lists PDFs in a Drive folder, downloads new ones to `pdfs/`, and triggers `ingest_pdf` for each.
- Add a CLI subcommand `python -m src.main sync-drive`.
- Skip until v1 works end-to-end.

---

## 12. Tests (minimum)

- `tests/test_scheduler.py` — SM-2 reference cases (forgot resets repetitions; correct grows interval; ease has 1.3 floor).
- `tests/test_quiz.py` — given a fixed RNG seed, `build_question` returns 4 unique options including the correct phrase.

No tests for LLM calls (network-bound, non-deterministic). Integration test = run `/quiz` end-to-end manually.

---

## 13. Setup steps (for the README)

```bash
git clone <repo> && cd english-assistant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # then fill in tokens
python -m src.main init
# Drop a PDF into pdfs/, then:
python -m src.main ingest pdfs/your-slides.pdf
python -m src.main fill-examples
python -m src.main run     # starts bot + 6 AM scheduler
```

To run the bot persistently: `systemd` user unit or `tmux` session. Sample unit:

```ini
[Unit]
Description=English Idiom Assistant
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/<user>/projects/english-assistant
ExecStart=/home/<user>/projects/english-assistant/.venv/bin/python -m src.main run
Restart=on-failure

[Install]
WantedBy=default.target
```

---

## 14. Open decisions for the implementer

- **Distractor quality.** Random idioms may be too easy if obviously off-topic. If accuracy stays > 95%, swap to LLM-generated plausible distractors (Haiku call per question, cached).
- **Multi-user.** Schema supports many users, but SM-2 state is per-idiom not per-user. If the bot grows past one user, add `chat_id` to `reviews` (composite PK).
- **Idiom dedup across PDFs.** `phrase` is UNIQUE — second PDF re-using the same idiom is silently skipped. If meanings differ, log a warning rather than overwrite.
- **Funny-example regeneration.** Add a `/regen <idiom>` admin command if the user dislikes a generated sentence.

---

## 15. Done criteria

- [ ] `python -m src.main init` creates the DB.
- [ ] Ingesting one sample PDF populates ≥ 10 idioms with meanings.
- [ ] `fill-examples` populates the `example` column for every idiom; idiom phrase appears verbatim in each.
- [ ] `/start` registers the chat_id.
- [ ] `/quiz` returns a 4-option question; tapping a button records the answer and updates `reviews`.
- [ ] At 06:00 local time, registered users receive 5 questions weighted toward weak idioms.
- [ ] `/stats` shows accuracy and weakest idioms.
- [ ] Repo pushed to GitHub (`AlbertPhan88/english-assistant`).
