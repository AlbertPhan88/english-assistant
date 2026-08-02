"""Second-pass Vietnamese review sweep using Sonnet 4.6 with the stricter prompt.

Run with: .venv/bin/python -m scripts.run_viet_review_sonnet
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
from anthropic import Anthropic

from src import config, db
from src.examples import REVIEW_VIET_PROMPT

MODEL = "claude-sonnet-4-6"
MAX_RPM = 30
_min_gap = 60.0 / MAX_RPM
_gate_lock = threading.Lock()
_next_allowed_at = 0.0


def _gate() -> None:
    global _next_allowed_at
    while True:
        with _gate_lock:
            now = time.monotonic()
            if now >= _next_allowed_at:
                _next_allowed_at = now + _min_gap
                return
            wait = _next_allowed_at - now
        time.sleep(wait)


def review_strict(phrase: str, meaning: str, viet: str, client: Anthropic) -> str:
    backoff = 5.0
    for attempt in range(6):
        _gate()
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=80,
                messages=[{"role": "user", "content": REVIEW_VIET_PROMPT.format(
                    phrase=phrase, meaning=meaning, viet=viet)}],
            )
            raw = resp.content[0].text.strip() if resp.content else ""
            # Take first non-empty line, strip quotes/dashes
            result = next(
                (l.strip().strip('"').lstrip("—").strip() for l in raw.splitlines() if l.strip()),
                "",
            )
            # Reject: empty, dash-only, too long, or contains sentence-connector words
            if (not result or result == "—" or len(result) > 100
                    or any(w in result for w in (" nhưng ", " tuy ", " bởi vì "))):
                return viet
            return result
        except anthropic.RateLimitError:
            print(f"  ! 429 on {phrase!r}, backing off {backoff:.0f}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 60.0)
    print(f"  ! gave up on {phrase!r} after 6 retries", flush=True)
    return viet


def main() -> int:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    with db.connect(config.DB_PATH) as conn:
        rows = list(conn.execute(
            "SELECT id, phrase, meaning, vietnamese_equiv FROM idioms "
            "WHERE vietnamese_equiv IS NOT NULL AND vietnamese_equiv != '' "
            "AND vietnamese_equiv != '—' ORDER BY id"
        ))
    total = len(rows)
    print(f"Sonnet-reviewing {total} idioms — rate limit {MAX_RPM} RPM, concurrency=4 ...", flush=True)
    t0 = time.monotonic()
    updated = 0
    done = 0

    def task(row: sqlite3.Row) -> tuple[int, str, str, str]:
        improved = review_strict(
            row["phrase"], row["meaning"], row["vietnamese_equiv"], client,
        )
        return row["id"], row["phrase"], row["vietnamese_equiv"], improved

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(task, r) for r in rows]
        for fut in as_completed(futures):
            idiom_id, phrase, old, new = fut.result()
            done += 1
            if new and new != old:
                with db.connect(config.DB_PATH) as conn:
                    db.update_vietnamese(conn, idiom_id, new)
                updated += 1
                print(f"  [{done}/{total}] {phrase!r}: {old!r} -> {new!r}", flush=True)
            if done % 50 == 0:
                elapsed = time.monotonic() - t0
                rate = done / elapsed if elapsed else 0
                eta = (total - done) / rate if rate else 0
                print(f"  ... {done}/{total} done ({updated} improved) — {elapsed:.0f}s elapsed, ETA {eta:.0f}s", flush=True)
    print(f"Done. {updated}/{total} improved in {time.monotonic()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
