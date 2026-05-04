from anthropic import Anthropic

from . import db
from .db import THEME_ORDER


THEME_LIST = THEME_ORDER

TAG_THEMES_PROMPT = """You are tagging English idioms with a single theme.

Theme list (choose exactly one per idiom):
{theme_list}

For each idiom below, output one line in the format:
id|theme

Idioms:
{idiom_lines}

Rules:
- Output ONLY the id|theme lines, one per idiom, nothing else.
- Use only themes from the list above.
- If unsure, use "general"."""


FUNNY_PROMPT = """For the idiom "{phrase}" (meaning: {meaning}), return exactly 2 lines:

LINE 1: One funny sentence (max 15 words, absurd/unexpected). The idiom must appear verbatim.
LINE 2: A Vietnamese idiom, proverb (tục ngữ), or folk saying (ca dao) with a similar meaning. Try hard — check idioms, proverbs, and common expressions. If no idiom exists, write a short Vietnamese phrase that captures the meaning (e.g. "nói vòng vo không đi vào vấn đề"). Never write "—".

No labels, no explanation. Just 2 lines."""

VIET_EQUIV_PROMPT = """English idiom: "{phrase}"
Meaning: {meaning}

Output ONE Vietnamese equivalent on a single line — in order of preference:
1. A Vietnamese idiom or proverb (tục ngữ/thành ngữ) with the same meaning
2. A Vietnamese folk saying (ca dao) that captures the idea
3. A short Vietnamese phrase used in everyday speech

One line only. No numbering, no explanation, no alternatives."""

STORY_PROMPT = """Write a 3-sentence funny mini-story that uses the idiom "{phrase}" (meaning: {meaning}).

Rules:
- The idiom must appear verbatim in the story
- Use the idiom naturally — never surround it with words that mean the same thing (e.g. don't write "he bribed someone to grease their palms" — that's redundant)
- Each sentence builds tension or absurdity
- Under 60 words total, no explanation of the idiom's meaning

Output only the story, nothing else."""

DAILY_STORY_PROMPT = """Write a short funny story (6-10 sentences, under 180 words) that naturally uses ALL of these English idioms verbatim:

{idiom_list}

Rules:
- Every idiom must appear in the story at least once, exactly as written
- Use each idiom naturally — never surround it with words that mean the same thing (e.g. don't write "he bribed someone to grease their palms" — that's redundant; just write "he greased their palms")
- When an idiom takes a person as object (e.g. "feel for someone"), use a specific pronoun or name, not a vague noun (e.g. "I feel for him" not "I feel for someone in his situation")
- The story should be absurd and entertaining, flowing naturally from one idiom to the next
- One continuous narrative — no separate paragraphs or sections

Output only the story, nothing else."""

TRANSLATE_PROMPT = """Translate the following English story to Vietnamese.

Idiom reference:
{idiom_map}

Rules:
- Write in natural, colloquial Vietnamese — like a Vietnamese author telling a funny story
- When an English idiom is used as a VERB PHRASE (e.g. "he beat around the bush"), replace it with its Vietnamese equivalent from the reference above
- When an English idiom is used as a NOUN or ADJECTIVE to describe someone/something (e.g. "Dave, the squeaky wheel"), translate its meaning naturally into Vietnamese (e.g. "Dave, tên hay kêu ca") — do NOT force a proverb into a noun slot
- Translate meaning and feeling, not word-for-word
- Keep the humor and absurdity of the original
- Do not add notes or explanations

Story:
{text}

Output only the Vietnamese translation, nothing else."""


REVIEW_VIET_PROMPT = """English idiom: "{phrase}" (meaning: {meaning})
Current Vietnamese: "{viet}"

Task: if "{viet}" is already a real Vietnamese idiom, proverb (tục ngữ/thành ngữ), or folk saying → output it unchanged.
If it is a naive literal translation or plain description → output a better Vietnamese idiom or proverb.

Output: one short Vietnamese phrase only. No explanation. No sentence. No dash."""


def _parse_response(text: str) -> tuple[str, str]:
    lines = [l.strip().strip('"') for l in text.strip().splitlines() if l.strip()]
    example = lines[0] if len(lines) > 0 else ""
    viet = lines[1] if len(lines) > 1 else "—"
    return example, viet


def generate_funny_example(phrase: str, meaning: str, client: Anthropic, model: str = "claude-haiku-4-5-20251001") -> tuple[str, str]:
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": FUNNY_PROMPT.format(phrase=phrase, meaning=meaning)}],
    )
    raw = resp.content[0].text if resp.content else ""
    return _parse_response(raw)


def fill_missing_examples(db_path: str, client: Anthropic) -> int:
    with db.connect(db_path) as conn:
        rows = db.idioms_missing_example(conn)
    filled = 0
    for row in rows:
        example, viet = generate_funny_example(row["phrase"], row["meaning"], client)
        if example:
            with db.connect(db_path) as conn:
                db.update_example(conn, row["id"], example)
                db.update_vietnamese(conn, row["id"], viet)
            filled += 1
    return filled


def generate_story(phrase: str, meaning: str, client: Anthropic, model: str = "claude-haiku-4-5-20251001") -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": STORY_PROMPT.format(phrase=phrase, meaning=meaning)}],
    )
    return resp.content[0].text.strip() if resp.content else ""


def fill_missing_stories(db_path: str, client: Anthropic) -> int:
    with db.connect(db_path) as conn:
        rows = db.idioms_missing_story(conn)
    filled = 0
    total = len(rows)
    for i, row in enumerate(rows, 1):
        story = generate_story(row["phrase"], row["meaning"], client)
        if story:
            with db.connect(db_path) as conn:
                db.update_story(conn, row["id"], story)
            filled += 1
        if i % 50 == 0:
            print(f"  {i}/{total} done ({filled} filled)...", flush=True)
    return filled


def translate_to_vietnamese(
    text: str,
    client: Anthropic,
    idioms: list[dict] | None = None,
    model: str = "claude-sonnet-4-6",
) -> str:
    if idioms:
        idiom_map = "\n".join(
            f'- "{i["phrase"]}" → {i["viet"]}'
            for i in idioms if i.get("viet") and i["viet"] != "—"
        ) or "(none available)"
    else:
        idiom_map = "(none available)"
    resp = client.messages.create(
        model=model,
        max_tokens=800,
        messages=[{"role": "user", "content": TRANSLATE_PROMPT.format(text=text, idiom_map=idiom_map)}],
    )
    return resp.content[0].text.strip() if resp.content else ""


def generate_daily_story(idioms: list[dict], client: Anthropic, model: str = "claude-haiku-4-5-20251001") -> str:
    idiom_list = "\n".join(f"- {i['phrase']} ({i['meaning']})" for i in idioms)
    resp = client.messages.create(
        model=model,
        max_tokens=400,
        messages=[{"role": "user", "content": DAILY_STORY_PROMPT.format(idiom_list=idiom_list)}],
    )
    return resp.content[0].text.strip() if resp.content else ""


def generate_vietnamese_equiv(phrase: str, meaning: str, client: Anthropic, model: str = "claude-haiku-4-5-20251001") -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=100,
        messages=[{"role": "user", "content": VIET_EQUIV_PROMPT.format(phrase=phrase, meaning=meaning)}],
    )
    raw = resp.content[0].text.strip() if resp.content else ""
    # Take first non-empty line only
    result = next((l.strip().strip('"') for l in raw.splitlines() if l.strip()), "")
    return result if result and result != "—" else ""


def review_vietnamese_equiv(phrase: str, meaning: str, viet: str, client: Anthropic, model: str = "claude-haiku-4-5-20251001") -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=80,
        messages=[{"role": "user", "content": REVIEW_VIET_PROMPT.format(phrase=phrase, meaning=meaning, viet=viet)}],
    )
    raw = resp.content[0].text.strip() if resp.content else ""
    result = next((l.strip().strip('"') for l in raw.splitlines() if l.strip()), "")
    # Reject explanations: too long, contains explanation words, or is empty/dash
    if not result or result == "—" or len(result) > 80 or any(w in result for w in ("là ", "được ", "nhưng ", "tuy ")):
        return viet
    return result


def review_all_vietnamese(db_path: str, client: Anthropic) -> int:
    with db.connect(db_path) as conn:
        rows = list(conn.execute(
            "SELECT id, phrase, meaning, vietnamese_equiv FROM idioms "
            "WHERE vietnamese_equiv IS NOT NULL AND vietnamese_equiv != '' AND vietnamese_equiv != '—'"
        ))
    updated = 0
    total = len(rows)
    for i, row in enumerate(rows, 1):
        improved = review_vietnamese_equiv(row["phrase"], row["meaning"], row["vietnamese_equiv"], client)
        if improved and improved != row["vietnamese_equiv"]:
            with db.connect(db_path) as conn:
                db.update_vietnamese(conn, row["id"], improved)
            updated += 1
        if i % 50 == 0:
            print(f"  {i}/{total} reviewed ({updated} improved)...", flush=True)
    return updated


def fill_missing_vietnamese(db_path: str, client: Anthropic) -> int:
    with db.connect(db_path) as conn:
        rows = db.idioms_missing_vietnamese(conn)
    filled = 0
    total = len(rows)
    for i, row in enumerate(rows, 1):
        viet = generate_vietnamese_equiv(row["phrase"], row["meaning"], client)
        if viet:
            with db.connect(db_path) as conn:
                db.update_vietnamese(conn, row["id"], viet)
            filled += 1
        if i % 50 == 0:
            print(f"  {i}/{total} done ({filled} filled)...", flush=True)
    return filled


def tag_themes_batch(batch: list, client: Anthropic, model: str = "claude-haiku-4-5-20251001") -> dict[int, str]:
    """Tag up to 20 idioms with a theme in one API call. Returns {id: theme}."""
    theme_list_str = ", ".join(THEME_LIST)
    idiom_lines = "\n".join(f"{row['id']}. {row['phrase']} — {row['meaning']}" for row in batch)
    prompt = TAG_THEMES_PROMPT.format(theme_list=theme_list_str, idiom_lines=idiom_lines)
    resp = client.messages.create(
        model=model,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip() if resp.content else ""
    results = {}
    for line in raw.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        id_str, theme = parts[0].strip(), parts[1].strip().lower()
        if not id_str.isdigit():
            continue
        if theme not in THEME_LIST:
            continue
        results[int(id_str)] = theme
    return results


def tag_all_themes(db_path: str, client: Anthropic) -> int:
    """Batch-tag all idioms missing a theme. Returns number tagged."""
    with db.connect(db_path) as conn:
        rows = db.idioms_missing_theme(conn)
    tagged = 0
    total = len(rows)
    batch_size = 20
    for i in range(0, total, batch_size):
        batch = rows[i:i + batch_size]
        try:
            results = tag_themes_batch(batch, client)
        except Exception as e:
            print(f"  Batch {i//batch_size + 1} failed: {e}", flush=True)
            continue
        with db.connect(db_path) as conn:
            for idiom_id, theme in results.items():
                db.update_theme(conn, idiom_id, theme)
                tagged += 1
        print(f"  {min(i + batch_size, total)}/{total} tagged ({tagged} so far)...", flush=True)
    return tagged
