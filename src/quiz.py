import logging
import random
import re
import sqlite3
from dataclasses import dataclass, field

from . import config, db

logger = logging.getLogger(__name__)


@dataclass
class Question:
    idiom_id: int
    stem: str
    options: list[str]
    correct_index: int
    kind: str = "forward"   # "forward" | "reverse" | "vietnamese" | "completion" | "production"
    phrase: str = ""        # shown in reverse/vietnamese question header
    reask: bool = False     # True if this is a re-ask of a previously missed idiom
    situation: str = ""     # For production questions: the situation used (for multi-turn drills)


_PLACEHOLDER = re.compile(
    r"^(someone'?s?|somebody'?s?|something|one'?s?|"
    r"oneself|yourself|himself|herself|itself|themselves|myself|ourselves|"
    r"you|one|they)$",
    re.IGNORECASE,
)

# Common irregular verb forms: base → [inflected, ...]
_IRREGULAR = {
    "be": ["is", "are", "was", "were", "been", "being"],
    "bite": ["bit", "bitten", "bites", "biting"],
    "break": ["broke", "broken", "breaks", "breaking"],
    "bring": ["brought", "brings", "bringing"],
    "buy": ["bought", "buys", "buying"],
    "catch": ["caught", "catches", "catching"],
    "come": ["came", "comes", "coming"],
    "cut": ["cuts", "cutting"],
    "do": ["did", "done", "does", "doing"],
    "fall": ["fell", "fallen", "falls", "falling"],
    "find": ["found", "finds", "finding"],
    "get": ["got", "gotten", "gets", "getting"],
    "give": ["gave", "given", "gives", "giving"],
    "go": ["went", "gone", "goes", "going"],
    "have": ["had", "has", "having"],
    "hit": ["hits", "hitting"],
    "hold": ["held", "holds", "holding"],
    "keep": ["kept", "keeps", "keeping"],
    "know": ["knew", "known", "knows", "knowing"],
    "lay": ["laid", "lays", "laying"],
    "leave": ["left", "leaves", "leaving"],
    "let": ["lets", "letting"],
    "lose": ["lost", "loses", "losing"],
    "make": ["made", "makes", "making"],
    "put": ["puts", "putting"],
    "run": ["ran", "runs", "running"],
    "say": ["said", "says", "saying"],
    "see": ["saw", "seen", "sees", "seeing"],
    "send": ["sent", "sends", "sending"],
    "sit": ["sat", "sits", "sitting"],
    "speak": ["spoke", "spoken", "speaks", "speaking"],
    "stand": ["stood", "stands", "standing"],
    "take": ["took", "taken", "takes", "taking"],
    "tell": ["told", "tells", "telling"],
    "think": ["thought", "thinks", "thinking"],
    "throw": ["threw", "thrown", "throws", "throwing"],
    "win": ["won", "wins", "winning"],
    "write": ["wrote", "written", "writes", "writing"],
}


_MODALS: dict[str, list[str]] = {
    "can": ["could", "cannot", "can't"],
    "will": ["would", "won't", "wouldn't"],
    "shall": ["should", "shouldn't"],
    "may": ["might"],
    "must": ["had to"],
}


def _verb_alternation(word: str) -> str:
    """Return a regex alternation matching base form + all known inflections."""
    base = word.lower()
    forms = [base] + _IRREGULAR.get(base, [])
    for suffix in ("s", "ed", "d", "ing"):
        candidate = base + suffix
        if candidate not in forms:
            forms.append(candidate)
    return "(?:" + "|".join(re.escape(f) for f in forms) + ")"


def _word_pattern(word: str, is_first: bool = False) -> str:
    """Regex pattern for one word in a phrase, handling placeholders and modals."""
    if _PLACEHOLDER.match(word):
        return r"\w+(?:'\w+)?"
    low = word.lower()
    if low in _MODALS:
        return "(?:" + "|".join(re.escape(f) for f in [low] + _MODALS[low]) + ")"
    if is_first:
        return _verb_alternation(word)
    return re.escape(word)


def _blank(text: str, phrase: str) -> str:
    words = phrase.split()

    # Step 1: exact match
    pattern = re.compile(re.escape(phrase), re.IGNORECASE)
    blanked, count = pattern.subn("___", text, count=1)
    if count > 0:
        return blanked

    # Step 2: placeholders as wildcards
    parts = [r"\w+(?:'\w+)?" if _PLACEHOLDER.match(w) else re.escape(w) for w in words]
    flex = re.compile(r"\s+".join(parts), re.IGNORECASE)
    blanked, count = flex.subn("___", text, count=1)
    if count > 0:
        return blanked

    # Step 3: inflect first word + handle modals/placeholders throughout
    if words:
        inflected_parts = [_word_pattern(w, i == 0) for i, w in enumerate(words)]
        inflect = re.compile(r"\s+".join(inflected_parts), re.IGNORECASE)
        blanked, count = inflect.subn("___", text, count=1)
        if count > 0:
            return blanked

    # No match found — caller should treat this as unusable
    return ""


_STEM_STOPWORDS = {
    "the", "a", "an", "to", "in", "of", "on", "at", "for", "with",
    "by", "up", "out", "off", "as", "is", "are", "was", "were",
}


def _has_enough_context(stem: str) -> bool:
    """Reject stems where the blank has fewer than 4 words of surrounding context."""
    return len(stem.replace("___", "").split()) >= 4


def _is_self_answering(stem: str, phrase: str) -> bool:
    """True if too many content words from the phrase are still visible in the stem."""
    content = [
        w.lower() for w in phrase.split()
        if w.lower() not in _STEM_STOPWORDS and not _PLACEHOLDER.match(w)
    ]
    if not content:
        return False
    cleaned = re.sub(r"___", "", stem).lower()
    matches = sum(
        1 for w in content if re.search(r"\b" + re.escape(w) + r"\b", cleaned)
    )
    return matches >= max(1, len(content) - 1)


def build_question(conn, idiom_row: sqlite3.Row, user_id: int = 0) -> Question:
    phrase = idiom_row["phrase"]

    # Rotate through pool of example sentences, fall back to column
    sentence = db.get_next_example(conn, idiom_row["id"], user_id)
    if not sentence:
        sentence = idiom_row["example"] or idiom_row["meaning"]
    if not sentence:
        raise ValueError(f"Idiom {phrase!r} has no example or meaning.")

    stem = _blank(sentence, phrase)
    if not stem or stem.strip() == "___" or not _has_enough_context(stem) or _is_self_answering(stem, phrase):
        # Example unusable — try story pool
        story = db.get_next_story(conn, idiom_row["id"], user_id) or idiom_row["story"] or ""
        stem = _blank(story, phrase) if story else ""
        if not stem or stem.strip() == "___" or not _has_enough_context(stem) or _is_self_answering(stem, phrase):
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


def build_reverse_question(conn, idiom_row: sqlite3.Row, user_id: int = 0) -> Question:
    phrase = idiom_row["phrase"]
    meaning = idiom_row["meaning"]
    # Rotate through story pool, fall back to column then example
    story = db.get_next_story(conn, idiom_row["id"], user_id)
    if not story:
        story = idiom_row["story"] or idiom_row["example"] or ""

    # The options are meanings, one of them correct, so a context that restates
    # the meaning hands over the answer. Falling back to the meaning itself used
    # to do exactly that. Raise instead: _build_one moves on to another type.
    if not story or meaning.strip().lower() in story.strip().lower():
        raise ValueError(f"Idiom {phrase!r} has no context that isn't its own meaning.")

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


# Hard cap on a situation, so one long line is trimmed rather than thrown away.
_SITUATION_MAX_CHARS = 400


SITUATION_PROMPT = """Invent ONE concrete, specific situation for a learner to use the English idiom "{phrase}" (meaning: {meaning}).

Rules:
- 1-2 sentences, under 35 words.
- Include specific details: named people, concrete objects, real stakes. NOT vague like "at work" or "with a friend".
- The situation should make the idiom feel natural to reach for — but do NOT include or hint at the idiom text itself in the situation.
- {avoid_clause}

Output only the situation text on a single line. No quotes, no headers, no "Situation:" prefix."""


def _generate_situation(phrase: str, meaning: str, avoid: list[str]) -> str | None:
    """Ask Claude for one concrete scenario, or None if it could not produce one.

    Returns None rather than a generic stand-in. A production question asks the
    learner to recall the idiom a situation calls for, so a vague situation makes
    the question unanswerable — "in a group conversation" fits every idiom in the
    database. Callers skip the question instead.
    """
    from anthropic import Anthropic
    from . import config
    try:
        client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
        avoid_clause = (
            "Do NOT repeat these already-used scenarios: " + " || ".join(avoid) + "."
        ) if avoid else "Come up with something fresh."
        resp = client.messages.create(
            model=config.BULK_MODEL,
            max_tokens=250,
            messages=[{"role": "user", "content": SITUATION_PROMPT.format(
                phrase=phrase, meaning=meaning, avoid_clause=avoid_clause,
            )}],
        )
        text = config.response_text(resp)
    except Exception as e:
        logger.warning("Situation generation failed for %r: %s", phrase, e)
        return None

    for line in text.splitlines():
        line = line.strip().strip('"').strip("'").strip()
        for prefix in ("Situation:", "Scenario:"):
            if line.lower().startswith(prefix.lower()):
                line = line[len(prefix):].strip()
        if not line:
            continue
        # Trim an over-long line at the last sentence end rather than discarding
        # it. A slightly long situation still teaches; a generic one does not.
        if len(line) > _SITUATION_MAX_CHARS:
            cut = line[:_SITUATION_MAX_CHARS]
            stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
            line = cut[:stop + 1] if stop > 80 else cut.rstrip() + "…"
        return line

    logger.warning(
        "Situation generation returned no usable line for %r: stop=%s raw=%r",
        phrase, resp.stop_reason, text[:200],
    )
    return None


def build_production_question(conn, idiom_row: sqlite3.Row, avoid_situations: list[str] | None = None) -> Question:
    phrase = idiom_row["phrase"]
    meaning = idiom_row["meaning"]
    situation = _generate_situation(phrase, meaning, avoid_situations or [])
    if situation is None:
        # Let _build_one fall through to a multiple-choice question for this
        # idiom rather than asking one with no usable situation.
        raise ValueError(f"No situation available for idiom {idiom_row['id']}")
    viet = idiom_row["vietnamese_equiv"] or ""
    viet_line = f"\n🇻🇳 {viet}" if viet and viet != "—" else ""
    stem = (
        f"Meaning: {meaning}{viet_line}\n\n"
        f"Situation: {situation}\n\n"
        "Recall the idiom that fits and use it in a sentence."
    )
    return Question(
        idiom_id=idiom_row["id"],
        stem=stem,
        options=[],
        correct_index=-1,
        kind="production",
        phrase=phrase,
        situation=situation,
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
    if not stem or not _has_enough_context(stem):
        return build_question(conn, idiom_row)
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
# SM-2 rotation: forward → production → reverse → production → completion
_KIND_BUILDERS = [
    # 0: forward
    [build_question, build_reverse_question],
    # 1: production (SM-2 early, ~day 10)
    [build_production_question, build_question, build_reverse_question],
    # 2: reverse
    [build_reverse_question, build_question],
    # 3: production (SM-2 mid, ~day 63)
    [build_production_question, build_question, build_reverse_question],
    # 4: completion
    [build_completion_question, build_question, build_reverse_question],
]

# Boot phase 0→forward, phase 1→reverse; phase 2 handled specially in build_one
_BOOT_KIND = [0, 2]


def build_one(conn, row, user_id: int = 0, allow_production: bool = True) -> Question:
    """Dispatch to the right question builder based on boot_phase or next_kind.

    With `allow_production` false the production builder is passed over and the
    idiom gets the next type in its chain instead. The idiom still gets asked —
    only the effort it demands changes — so a session keeps its question count.
    """
    boot_phase = row["boot_phase"] if row["boot_phase"] is not None else -1
    if 0 <= boot_phase <= 2:
        if boot_phase == 2:
            if allow_production:
                return build_production_question(conn, row)
            # Phase 2 has no chain of its own; reuse the reverse-slot chain so a
            # boot idiom over the cap is still reviewed.
            kind_idx = 2
        else:
            kind_idx = _BOOT_KIND[boot_phase]
    else:
        kind_idx = (row["next_kind"] or 0) % 5

    for builder in _KIND_BUILDERS[kind_idx]:
        if builder is build_production_question and not allow_production:
            continue
        try:
            sig = builder.__code__.co_varnames[:builder.__code__.co_argcount]
            if "user_id" in sig:
                return builder(conn, row, user_id)
            return builder(conn, row)
        except ValueError:
            continue
    raise ValueError(f"Could not build any question for idiom {row['id']}")


def build_questions_from_rows(conn, rows: list, user_id: int = 0,
                              max_production: int | None = None) -> list[Question]:
    """Build a question per row, capping how many demand a written sentence.

    Production questions cost far more effort than a multiple-choice tap, so a
    set that fills with them is exhausting at an unchanged question count. Rows
    past the cap fall through to a cheaper type rather than being dropped.
    """
    questions = []
    produced = 0
    for row in rows:
        allow = max_production is None or produced < max_production
        try:
            q = build_one(conn, row, user_id, allow_production=allow)
        except ValueError:
            continue
        if q.kind == "production":
            produced += 1
        questions.append(q)
    return questions


def build_daily_set(conn, n: int, user_id: int = 0) -> list[Question]:
    from . import config
    today = config.today_local()
    rows = db.build_daily_rows(conn, today, n, user_id)
    return build_questions_from_rows(conn, rows, user_id)
