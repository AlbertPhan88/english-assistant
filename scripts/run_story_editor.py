"""Apply the editor pass to every stored daily_stories row.

Run with: .venv/bin/python -m scripts.run_story_editor
"""
from __future__ import annotations

import sys
import time

import anthropic
from anthropic import Anthropic

from src import config, db
from src.examples import edit_vietnamese_story


def main() -> int:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    with db.connect(config.DB_PATH) as conn:
        rows = list(conn.execute(
            "SELECT date, story, story_vi FROM daily_stories "
            "WHERE story_vi IS NOT NULL AND story_vi != '' ORDER BY date"
        ))
    total = len(rows)
    print(f"Editing {total} stored daily stories ...", flush=True)
    t0 = time.monotonic()
    edited = 0
    for i, row in enumerate(rows, 1):
        date = row["date"]
        backoff = 5.0
        for attempt in range(6):
            try:
                new_vi = edit_vietnamese_story(row["story"], row["story_vi"], client)
                break
            except anthropic.RateLimitError:
                print(f"  ! 429 on {date}, backing off {backoff:.0f}s", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)
        else:
            print(f"  ! gave up on {date}", flush=True)
            continue
        if new_vi and new_vi != row["story_vi"]:
            with db.connect(config.DB_PATH) as conn:
                conn.execute(
                    "UPDATE daily_stories SET story_vi = ? WHERE date = ?",
                    (new_vi, date),
                )
            edited += 1
            print(f"  [{i}/{total}] {date}: updated ({len(row['story_vi'])} -> {len(new_vi)} chars)", flush=True)
        else:
            print(f"  [{i}/{total}] {date}: unchanged", flush=True)
    print(f"Done. {edited}/{total} updated in {time.monotonic()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
