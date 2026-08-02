"""Normalize idiom phrases: rewrite slot-fillable possessives to canonical form.

Uses Claude to classify each candidate as fossilized or slot-fillable, and to
propose a canonical 'one's'/'someone's' rewrite when slot-fillable.

By default runs in --dry-run mode and writes proposals to stdout.
Pass --apply to commit changes to the DB.

Run:
    .venv/bin/python -m scripts.normalize_idiom_pronouns        # dry-run
    .venv/bin/python -m scripts.normalize_idiom_pronouns --apply
"""
from __future__ import annotations

import re
import sys

import anthropic
from anthropic import Anthropic

from src import config, db

PROMPT = """You are normalizing English idiom dictionary entries.

The headword is currently: "{phrase}"
Meaning: {meaning}

Decide whether the possessive pronoun (my/his/her/your/our/their) in the phrase is:
(A) FOSSILIZED — locked as part of the idiom, doesn't vary by speaker
    Examples: "in my humble opinion", "in your face", "my bad", "over my dead body"
(B) SLOT-FILLABLE — varies based on who the possessor is
    Examples: "follow my lead" → "follow one's lead", "wrap your head around" → "wrap one's head around"

If (A) FOSSILIZED → output a single line: KEEP
If (B) SLOT-FILLABLE → output a single line: REWRITE: <canonical form using "one's" for self-referring possessives, or "someone's" for other-referring>

Rules for the canonical form:
- Use "one's" when the possessor is the subject (e.g. "wrap one's head around")
- Use "someone's" when the possessor is an object/patient (e.g. "hold a special place in someone's affections")
- Lower-case unless proper noun
- No quotes, no explanation, just the rewrite

Output ONE line only."""


def classify(phrase: str, meaning: str, client: Anthropic) -> tuple[str, str | None]:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=60,
        messages=[{"role": "user", "content": PROMPT.format(phrase=phrase, meaning=meaning)}],
    )
    raw = resp.content[0].text.strip() if resp.content else ""
    line = next((l.strip() for l in raw.splitlines() if l.strip()), "")
    upper = line.upper()
    if upper.startswith("KEEP"):
        return "keep", None
    if upper.startswith("REWRITE:"):
        new = line.split(":", 1)[1].strip().strip('"').strip()
        return "rewrite", new
    return "unknown", line


def main() -> int:
    apply = "--apply" in sys.argv
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    with db.connect(config.DB_PATH) as conn:
        rows = list(conn.execute("SELECT id, phrase, meaning FROM idioms"))
    pat = re.compile(r"\b(my|his|her|your|our|their)\b", re.IGNORECASE)
    candidates = [r for r in rows if pat.search(r["phrase"])]
    print(f"Candidates with possessive pronouns: {len(candidates)}", flush=True)
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}\n", flush=True)

    decisions = []
    for r in candidates:
        verdict, new = classify(r["phrase"], r["meaning"], client)
        decisions.append((r["id"], r["phrase"], verdict, new))
        if verdict == "keep":
            print(f"  KEEP    id={r['id']:5} {r['phrase']!r}", flush=True)
        elif verdict == "rewrite" and new:
            print(f"  REWRITE id={r['id']:5} {r['phrase']!r} -> {new!r}", flush=True)
        else:
            print(f"  ?       id={r['id']:5} {r['phrase']!r}  raw={new!r}", flush=True)

    if not apply:
        n_rewrite = sum(1 for _, _, v, _ in decisions if v == "rewrite")
        n_keep = sum(1 for _, _, v, _ in decisions if v == "keep")
        print(f"\nDry-run summary: {n_keep} keep, {n_rewrite} rewrite. Re-run with --apply to commit.", flush=True)
        return 0

    # Apply: check for collisions first
    with db.connect(config.DB_PATH) as conn:
        applied = 0
        for idiom_id, old, verdict, new in decisions:
            if verdict != "rewrite" or not new:
                continue
            collision = conn.execute(
                "SELECT id FROM idioms WHERE LOWER(phrase) = LOWER(?) AND id != ?",
                (new, idiom_id),
            ).fetchone()
            if collision:
                print(f"  ! collision: {new!r} already exists as id={collision[0]}; skipping id={idiom_id}", flush=True)
                continue
            conn.execute("UPDATE idioms SET phrase = ? WHERE id = ?", (new, idiom_id))
            applied += 1
        print(f"\nApplied {applied} rewrites.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
