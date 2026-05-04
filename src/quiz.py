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


_PLACEHOLDER = re.compile(r"^(someone'?s?|somebody'?s?|something|one'?s?)$", re.IGNORECASE)


def _blank(text: str, phrase: str) -> str:
    # Exact match first
    pattern = re.compile(re.escape(phrase), re.IGNORECASE)
    blanked, count = pattern.subn("___", text, count=1)
    if count > 0:
        return blanked

    # Retry treating placeholder words (someone, something, etc.) as wildcards
    parts = [r"\w+(?:'\w+)?" if _PLACEHOLDER.match(w) else re.escape(w) for w in phrase.split()]
    flex = re.compile(r"\s+".join(parts), re.IGNORECASE)
    blanked, count = flex.subn("___", text, count=1)
    if count > 0:
        return blanked

    return f"{text}\n\n(Fill in: ___)"


def build_question(conn, idiom_row: sqlite3.Row) -> Question:
    phrase = idiom_row["phrase"]
    example = idiom_row["example"] or idiom_row["meaning"]
    if not example:
        raise ValueError(f"Idiom {phrase!r} has no example or meaning.")

    stem = _blank(example, phrase)
    # If blanking left no context (example was just the phrase), try story
    if stem.strip() == "___":
        story = idiom_row["story"] if idiom_row["story"] else ""
        if story:
            stem = _blank(story, phrase)
        if not story or stem.strip() == "___":
            raise ValueError(f"Idiom {phrase!r} has no usable fill-in context.")

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


def _find_sentence(story: str, phrase: str) -> str | None:
    """Return the sentence from story that contains phrase, or None."""
    sentences = re.split(r'(?<=[.!?])\s+', story.strip())
    pattern = re.compile(re.escape(phrase), re.IGNORECASE)
    for sent in sentences:
        if pattern.search(sent):
            return sent.strip()
    return None


def build_question_from_story(conn, idiom_row: sqlite3.Row, story: str) -> Question:
    """Like build_question but uses the sentence from the daily story as the stem."""
    phrase = idiom_row["phrase"]
    sentence = _find_sentence(story, phrase)
    if not sentence:
        return build_question(conn, idiom_row)

    stem = _blank(sentence, phrase)
    distractors = db.random_distractor_idioms(conn, idiom_row["id"], 3)
    if len(distractors) < 3:
        raise ValueError("Not enough idioms for distractors.")

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


def build_questions_from_rows(conn, rows: list) -> list[Question]:
    """Build questions from a pre-fetched list of idiom rows.
    Every 3rd question is a reverse quiz (only if story or example available).
    """
    questions = []
    for i, row in enumerate(rows):
        try:
            if i % 3 == 2 and (row["story"] or row["example"]):
                questions.append(build_reverse_question(conn, row))
            else:
                questions.append(build_question(conn, row))
        except ValueError:
            try:
                questions.append(build_reverse_question(conn, row))
            except ValueError:
                continue
    return questions


def build_daily_set(conn, n: int) -> list[Question]:
    today = date.today()
    rows = db.build_daily_rows(conn, today, n)
    return build_questions_from_rows(conn, rows)
