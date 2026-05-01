import random
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from . import db


@dataclass
class Question:
    idiom_id: int
    stem: str
    options: list[str]
    correct_index: int


def _blank(text: str, phrase: str) -> str:
    """Replace idiom phrase in text with ___."""
    pattern = re.compile(re.escape(phrase), re.IGNORECASE)
    blanked, count = pattern.subn("___", text, count=1)
    if count == 0:
        blanked = f"{text}\n\n(Fill in: ___)"
    return blanked


def build_question(conn, idiom_row: sqlite3.Row) -> Question:
    phrase = idiom_row["phrase"]
    example = idiom_row["example"] or idiom_row["meaning"]
    if not example:
        raise ValueError(f"Idiom {phrase!r} has no example or meaning.")

    stem = _blank(example, phrase)

    distractors = db.random_distractor_idioms(conn, idiom_row["id"], 3)
    if len(distractors) < 3:
        raise ValueError("Not enough idioms in DB to build distractors. Ingest more PDFs first.")

    options = [d["phrase"] for d in distractors] + [phrase]
    random.shuffle(options)
    correct_index = options.index(phrase)

    return Question(
        idiom_id=idiom_row["id"],
        stem=stem,
        options=options,
        correct_index=correct_index,
    )


def build_daily_set(conn, n: int) -> list[Question]:
    today = date.today()
    rows = db.due_idioms(conn, today, n)
    questions = []
    for row in rows:
        try:
            questions.append(build_question(conn, row))
        except ValueError:
            continue
    return questions
