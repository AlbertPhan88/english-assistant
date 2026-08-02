"""Fix the 19 idioms whose stored Vietnamese is actually the LLM's chain-of-thought.

Strategy: regenerate the Vietnamese using VIET_EQUIV_PROMPT (simpler — just asks
for one Vietnamese phrase, no "run four checks" wording). Then validate the output
against a leak-detector before saving.

Run: .venv/bin/python -m scripts.fix_leaked_viet
"""
from __future__ import annotations

import re
import sys
import time

import anthropic
from anthropic import Anthropic

from src import config, db
from src.examples import VIET_EQUIV_PROMPT

LEAK_PATTERNS = [
    r"let me\b",
    r"check(?:ing|s)?\b.*[:.]",
    r"^check\b",
    r"\bgrammar\s*[:\-]",
    r"\bmeaning\s*[:\-]",
    r"\bauthenticity\s*[:\-]",
    r"\bnaturalness\s*[:\-]",
    r"\bfour\s+(checks|criteria)\b",
    r"\bcheck \d\b",
    r"\bvietnamese\s*[:\-]",
]
LEAK_RE = re.compile("|".join(LEAK_PATTERNS), re.IGNORECASE)


def looks_leaked(text: str) -> bool:
    return bool(LEAK_RE.search(text)) or len(text) > 100


def regen(phrase: str, meaning: str, client: Anthropic) -> str:
    """Use the simple equiv-prompt (no 'four checks' wording) so the model
    doesn't have an instruction template to echo."""
    backoff = 5.0
    for attempt in range(5):
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=80,
                messages=[{"role": "user", "content": VIET_EQUIV_PROMPT.format(
                    phrase=phrase, meaning=meaning)}],
            )
            raw = resp.content[0].text.strip() if resp.content else ""
            for line in raw.splitlines():
                line = line.strip().strip('"').strip("—").strip()
                if not line or looks_leaked(line):
                    continue
                return line
            return ""
        except anthropic.RateLimitError:
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 60.0)
    return ""


def main() -> int:
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    with db.connect(config.DB_PATH) as conn:
        rows = list(conn.execute(
            "SELECT id, phrase, meaning, vietnamese_equiv FROM idioms "
            "WHERE vietnamese_equiv IS NOT NULL AND vietnamese_equiv != ''"
        ))
    leaked = [r for r in rows if looks_leaked(r["vietnamese_equiv"])]
    print(f"Found {len(leaked)} leaked rows", flush=True)
    fixed = 0
    for r in leaked:
        new = regen(r["phrase"], r["meaning"], client)
        if new and not looks_leaked(new):
            with db.connect(config.DB_PATH) as conn:
                db.update_vietnamese(conn, r["id"], new)
            fixed += 1
            print(f"  id={r['id']:5} {r['phrase']!r:35}: {r['vietnamese_equiv']!r} -> {new!r}", flush=True)
        else:
            print(f"  ! id={r['id']} {r['phrase']!r}: could not regenerate ({new!r})", flush=True)
    print(f"Done. {fixed}/{len(leaked)} fixed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
