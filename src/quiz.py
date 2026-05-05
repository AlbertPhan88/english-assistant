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
    kind: str = "forward"   # "forward" | "reverse" | "vietnamese" | "completion"
    phrase: str = ""        # shown in reverse/vietnamese question header
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

    # Rotate through pool of example sentences, fall back to column
    sentence = db.get_next_example(conn, idiom_row["id"])
    if not sentence:
        sentence = idiom_row["example"] or idiom_row["meaning"]
    if not sentence:
        raise ValueError(f"Idiom {phrase!r} has no example or meaning.")

    stem = _blank(sentence, phrase)
    # If blanking left no context, try story pool
    if stem.strip() == "___":
        story = db.get_next_story(conn, idiom_row["id"]) or idiom_row["story"] or ""
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
    # Rotate through story pool, fall back to column then example
    story = db.get_next_story(conn, idiom_row["id"])
    if not story:
        story = idiom_row["story"] or idiom_row["example"] or meaning

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


def build_vietnamese_question(conn, idiom_row: sqlite3.Row) -> Question:
    phrase = idiom_row["phrase"]
    viet = idiom_row["vietnamese_equiv"] if idiom_row["vietnamese_equiv"] else ""
    if not viet or viet == "—":
        raise ValueError(f"Idiom {phrase!r} has no Vietnamese equivalent.")

    distractors = db.random_distractor_idioms(conn, idiom_row["id"], 3)
    if len(distractors) < 3:
        raise ValueError("Not enough idioms in DB to build distractors.")

    options = [d["phrase"] for d in distractors] + [phrase]
    random.shuffle(options)
    correct_index = options.index(phrase)

    return Question(
        idiom_id=idiom_row["id"],
        stem=f"Tương đương tiếng Việt: {viet}",
        options=options,
        correct_index=correct_index,
        kind="vietnamese",
        phrase=phrase,
    )


def build_completion_question(conn, idiom_row: sqlite3.Row) -> Question:
    phrase = idiom_row["phrase"]
    words = phrase.split()
    if len(words) < 2:
        raise ValueError(f"Idiom {phrase!r} too short for completion question.")

    # Split: show all but last word, complete with last word
    first_part = " ".join(words[:-1])
    last_word = words[-1].lower()

    distractor_words = db.random_distractor_last_words(conn, idiom_row["id"], 3)
    if len(distractor_words) < 3:
        raise ValueError("Not enough idioms in DB to build completion distractors.")

    options = distractor_words + [last_word]
    random.shuffle(options)
    correct_index = options.index(last_word)

    return Question(
        idiom_id=idiom_row["id"],
        stem=f"Complete the idiom:\n\n{first_part} ___",
        options=options,
        correct_index=correct_index,
        kind="completion",
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


# Builders in fallback order for each kind index
_KIND_BUILDERS = [
    # 0: forward
    [build_question, build_reverse_question],
    # 1: reverse
    [build_reverse_question, build_question],
    # 2: vietnamese
    [build_vietnamese_question, build_reverse_question, build_question],
    # 3: completion
    [build_completion_question, build_question, build_reverse_question],
]

# Boot camp phase → kind index (phase 2 tries vietnamese, falls back via _KIND_BUILDERS)
_BOOT_KIND = [0, 1, 2]


def _build_one(conn, row) -> Question:
    """Dispatch to the right question builder based on boot_phase or next_kind."""
    boot_phase = row["boot_phase"] if row["boot_phase"] is not None else -1
    if 0 <= boot_phase <= 2:
        kind_idx = _BOOT_KIND[boot_phase]
    else:
        kind_idx = (row["next_kind"] or 0) % 4

    for builder in _KIND_BUILDERS[kind_idx]:
        try:
            return builder(conn, row)
        except ValueError:
            continue
    raise ValueError(f"Could not build any question for idiom {row['id']}")


def build_questions_from_rows(conn, rows: list) -> list[Question]:
    questions = []
    for row in rows:
        try:
            questions.append(_build_one(conn, row))
        except ValueError:
            continue
    return questions


def build_daily_set(conn, n: int) -> list[Question]:
    today = date.today()
    rows = db.build_daily_rows(conn, today, n)
    return build_questions_from_rows(conn, rows)
