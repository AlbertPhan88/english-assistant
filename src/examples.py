from anthropic import Anthropic

from . import db


FUNNY_PROMPT = """For the idiom "{phrase}" (meaning: {meaning}), return exactly 2 lines:

LINE 1: One funny sentence (max 15 words, absurd/unexpected). The idiom must appear verbatim.
LINE 2: Vietnamese equivalent idiom or proverb (just the phrase, e.g. "Nước đổ lá khoai"). Write "—" if none exists.

No labels, no explanation. Just 2 lines."""

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
- The story should be absurd and entertaining, flowing naturally from one idiom to the next
- One continuous narrative — no separate paragraphs or sections

Output only the story, nothing else."""

TRANSLATE_PROMPT = """Translate the following English story to Vietnamese. Keep it natural and colloquial — translate meaning, not word-for-word. Where an English idiom has a Vietnamese equivalent, use it.

{text}

Output only the Vietnamese translation, nothing else."""


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


def translate_to_vietnamese(text: str, client: Anthropic, model: str = "claude-haiku-4-5-20251001") -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=500,
        messages=[{"role": "user", "content": TRANSLATE_PROMPT.format(text=text)}],
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


def fill_missing_vietnamese(db_path: str, client: Anthropic) -> int:
    with db.connect(db_path) as conn:
        rows = db.idioms_missing_vietnamese(conn)
    filled = 0
    total = len(rows)
    for i, row in enumerate(rows, 1):
        _, viet = generate_funny_example(row["phrase"], row["meaning"], client)
        if viet:
            with db.connect(db_path) as conn:
                db.update_vietnamese(conn, row["id"], viet)
            filled += 1
        if i % 50 == 0:
            print(f"  {i}/{total} done ({filled} filled)...", flush=True)
    return filled
