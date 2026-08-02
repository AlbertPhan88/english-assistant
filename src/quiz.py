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


# Static fallback situations — used if the LLM call fails.
_SITUATIONS = [
    "chatting with a friend",
    "at work discussing a problem",
    "texting someone about something uncomfortable",
    "talking to your boss or teacher",
    "during a disagreement with someone",
    "catching up with a colleague",
    "in a group conversation",
]


SITUATION_PROMPT = """Invent ONE concrete, specific situation for a learner to use the English idiom "{phrase}" (meaning: {meaning}).

Rules:
- 1-2 sentences, under 35 words.
- Include specific details: named people, concrete objects, real stakes. NOT vague like "at work" or "with a friend".
- The situation should make the idiom feel natural to reach for — but do NOT include or hint at the idiom text itself in the situation.
- {avoid_clause}

Output only the situation text on a single line. No quotes, no headers, no "Situation:" prefix."""


def _generate_situation(phrase: str, meaning: str, avoid: list[str]) -> str:
    """Ask Claude for one concrete scenario. Falls back to a static one on failure."""
    try:
        from anthropic import Anthropic
        from . import config
        client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
        avoid_clause = (
            "Do NOT repeat these already-used scenarios: " + " || ".join(avoid) + "."
        ) if avoid else "Come up with something fresh."
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=90,
            messages=[{"role": "user", "content": SITUATION_PROMPT.format(
                phrase=phrase, meaning=meaning, avoid_clause=avoid_clause,
            )}],
        )
        text = resp.content[0].text.strip() if resp.content else ""
        # Take first non-empty line, strip quotes/prefix
        for line in text.splitlines():
            line = line.strip().strip('"').strip("'").strip()
            for prefix in ("Situation:", "Scenario:"):
                if line.lower().startswith(prefix.lower()):
                    line = line[len(prefix):].strip()
            if line and len(line) < 250:
                return line
    except Exception:
        pass
    # Fallback: pick a static situation that isn't in `avoid`
    remaining = [s for s in _SITUATIONS if s not in avoid]
    return random.choice(remaining or _SITUATIONS)


def build_production_question(conn, idiom_row: sqlite3.Row, avoid_situations: list[str] | None = None) -> Question:
    phrase = idiom_row["phrase"]
    meaning = idiom_row["meaning"]
    situation = _generate_situation(phrase, meaning, avoid_situations or [])
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

# Boot phase 0→forward, phase 1→reverse; phase 2 handled specially in _build_one
_BOOT_KIND = [0, 2]


def _build_one(conn, row, user_id: int = 0) -> Question:
    """Dispatch to the right question builder based on boot_phase or next_kind."""
    boot_phase = row["boot_phase"] if row["boot_phase"] is not None else -1
    if 0 <= boot_phase <= 2:
        if boot_phase == 2:
            return build_production_question(conn, row)
        kind_idx = _BOOT_KIND[boot_phase]
    else:
        kind_idx = (row["next_kind"] or 0) % 5

    for builder in _KIND_BUILDERS[kind_idx]:
        try:
            sig = builder.__code__.co_varnames[:builder.__code__.co_argcount]
            if "user_id" in sig:
                return builder(conn, row, user_id)
            return builder(conn, row)
        except ValueError:
            continue
    raise ValueError(f"Could not build any question for idiom {row['id']}")


def build_questions_from_rows(conn, rows: list, user_id: int = 0) -> list[Question]:
    questions = []
    for row in rows:
        try:
            questions.append(_build_one(conn, row, user_id))
        except ValueError:
            continue
    return questions


def build_daily_set(conn, n: int, user_id: int = 0) -> list[Question]:
    today = date.today()
    rows = db.build_daily_rows(conn, today, n, user_id)
    return build_questions_from_rows(conn, rows, user_id)
