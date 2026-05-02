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
    kind: str = "forward"   # "forward" | "reverse"
    phrase: str = ""        # shown in reverse question header
    reask: bool = False     # True if this is a re-ask of a previously missed idiom


def _blank(text: str, phrase: str) -> str:
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
        kind="forward",
        phrase=phrase,
    )


def build_reverse_question(conn, idiom_row: sqlite3.Row) -> Question:
    phrase = idiom_row["phrase"]
    meaning = idiom_row["meaning"]
    story = idiom_row["story"] if idiom_row["story"] else (idiom_row["example"] or meaning)

    distractors = db.random_distractor_meanings(conn, idiom_row["id"], 3)
    if len(distractors) < 3:
        raise ValueError("Not enough idioms in DB to build distractors.")

    options = [d["meaning"] for d in distractors] + [meaning]
    random.shuffle(options)
    correct_index = options.index(meaning)

    return Question(
        idiom_id=idiom_row["id"],
        stem=f'"{story}"',
        options=options,
        correct_index=correct_index,
        kind="reverse",
        phrase=phrase,
    )


def build_daily_set(conn, n: int) -> list[Question]:
    today = date.today()
    rows = db.due_idioms(conn, today, n)
    questions = []
    for i, row in enumerate(rows):
        try:
            # Every 3rd question is a reverse quiz (only if story or example exists)
            if i % 3 == 2 and (row["story"] or row["example"]):
                questions.append(build_reverse_question(conn, row))
            else:
                questions.append(build_question(conn, row))
        except ValueError:
            continue
    return questions
