"""Tag idioms for tone and register.

Applies the five-axis scheme from the "Living the Language" methods handout
(pp. 46-47): formality, medium, domain, flavour, connotation. See
examples.REGISTER_AXES for the allowed codes.

By default runs in dry-run mode and prints what it would store.
Pass --apply to commit tags to the DB.

Run:
    .venv/bin/python -m scripts.tag_register            # dry-run
    .venv/bin/python -m scripts.tag_register --apply
    .venv/bin/python -m scripts.tag_register --limit 40 # try a small slice
"""
from __future__ import annotations

import sys

from anthropic import Anthropic

from src import config, db
from src.examples import format_register, tag_register_batch

BATCH = 20


def main() -> int:
    apply = "--apply" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    with db.connect(config.DB_PATH) as conn:
        rows = db.idioms_missing_register(conn)
    if limit:
        rows = rows[:limit]
    print(f"Idioms missing register tags: {len(rows)}", flush=True)
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}\n", flush=True)

    tagged = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start:start + BATCH]
        try:
            results = tag_register_batch(batch, client)
        except Exception as e:
            print(f"  ! batch at {start} failed: {e}", flush=True)
            continue
        for row in batch:
            tags = results.get(row["id"])
            if not tags:
                print(f"  ?      id={row['id']:5} {row['phrase']!r} — no valid tag", flush=True)
                continue
            print(
                f"  tagged id={row['id']:5} {row['phrase']!r}: "
                f"{format_register(tags, symbols=False)}",
                flush=True,
            )
        if apply and results:
            with db.connect(config.DB_PATH) as conn:
                for idiom_id, tags in results.items():
                    db.update_register(conn, idiom_id, tags)
        tagged += len(results)
        done = min(start + BATCH, len(rows))
        print(f"  -- {done}/{len(rows)} processed, {tagged} tagged", flush=True)

    if not apply:
        print(f"\nDry-run: {tagged} would be tagged. Re-run with --apply to commit.", flush=True)
    else:
        print(f"\nApplied {tagged} register tags.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
