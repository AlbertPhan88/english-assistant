from anthropic import Anthropic

from . import db


FUNNY_PROMPT = """Write ONE short funny example sentence (max 25 words) using the idiom "{phrase}" — meaning: {meaning}.

Rules:
- The idiom must appear verbatim in the sentence.
- Make it memorable: absurd scenario, unexpected twist, or silly character.
- No explanation, no quotes around the sentence, no preamble. Just the sentence."""


def generate_funny_example(phrase: str, meaning: str, client: Anthropic, model: str = "claude-haiku-4-5-20251001") -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": FUNNY_PROMPT.format(phrase=phrase, meaning=meaning)}],
    )
    return resp.content[0].text.strip().strip('"') if resp.content else ""


def fill_missing_examples(db_path: str, client: Anthropic) -> int:
    filled = 0
    with db.connect(db_path) as conn:
        rows = db.idioms_missing_example(conn)
        for row in rows:
            example = generate_funny_example(row["phrase"], row["meaning"], client)
            if example:
                db.update_example(conn, row["id"], example)
                filled += 1
    return filled
