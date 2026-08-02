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

EXTRA_EXAMPLE_PROMPT = """For the idiom "{phrase}" (meaning: {meaning}), write ONE funny example sentence (max 15 words, absurd/unexpected). The idiom must appear verbatim. Replace placeholder words like "one's", "someone's", "someone", "somebody" with a specific name or pronoun. Output only the sentence, nothing else."""

EXTRA_STORY_PROMPT = """Write a 3-sentence funny mini-story that uses the idiom "{phrase}" (meaning: {meaning}).

Rules:
- The idiom must appear verbatim in the story
- Use the idiom naturally — never surround it with words that mean the same thing
- Replace placeholder words like "one's", "someone's", "someone", "somebody" with a specific name or pronoun based on the character (e.g. "his praise", "her patience", "Tim's advice")
- Each sentence builds tension or absurdity
- Under 60 words total, no explanation of the idiom's meaning
- Must be DIFFERENT in setting and characters from: "{existing_story}"

Output only the story, nothing else."""

STORY_PROMPT = """Write a 3-sentence funny mini-story that uses the idiom "{phrase}" (meaning: {meaning}).

Rules:
- The idiom must appear verbatim in the story
- Use the idiom naturally — never surround it with words that mean the same thing (e.g. don't write "he bribed someone to grease their palms" — that's redundant)
- Replace placeholder words like "one's", "someone's", "someone", "somebody" with a specific name or pronoun based on the character (e.g. "his praise", "her patience", "Tim's advice")
- Each sentence builds tension or absurdity
- Under 60 words total, no explanation of the idiom's meaning

Output only the story, nothing else."""

DAILY_STORY_PROMPT = """Write a short funny story (6-10 sentences, under 180 words) that naturally uses ALL of these English idioms verbatim:

{idiom_list}

Rules:
- Every idiom must appear in the story at least once, exactly as written
- Use each idiom naturally — never surround it with words that mean the same thing (e.g. don't write "he bribed someone to grease their palms" — that's redundant; just write "he greased their palms")
- Replace placeholder words like "one's", "someone's", "someone", "somebody" with a specific name or pronoun based on the character (e.g. "his praise", "her patience", "Tim's advice")
- When an idiom takes a person as object (e.g. "feel for someone"), use a specific pronoun or name, not a vague noun (e.g. "I feel for him" not "I feel for someone in his situation")
- The story should be absurd and entertaining, flowing naturally from one idiom to the next
- One continuous narrative — no separate paragraphs or sections

Output only the story, nothing else."""

TRANSLATE_PROMPT = """Translate the following English story to Vietnamese.

Idiom reference (UNRELIABLE HINTS — see rules below):
{idiom_map}

CORE PRINCIPLE: The reader must understand every sentence. If using a hint would make the sentence ungrammatical, confusing, or shift the meaning — DROP THE HINT and paraphrase naturally in your own Vietnamese. A clear paraphrase always beats a forced "idiom".

Treat each hint with suspicion. Use it ONLY if all four checks pass:
1. GRAMMAR: it slots cleanly into the sentence with correct Vietnamese grammar.
2. MEANING: it carries the same meaning as the English idiom in THIS context (not just a related vibe).
3. AUTHENTICITY: it is a real Vietnamese expression — NOT a calque of another English idiom (e.g. "Bão tố trong cốc nước" is just "storm in a teacup" word-for-word; reject it).
4. NATURALNESS: it reads like something a native Vietnamese speaker would actually say in this context.

If ANY check fails → paraphrase the MEANING in natural Vietnamese instead. Examples of when to drop a hint:
- "be frowned upon" hint = "Bị coi thường không" → broken grammar (trailing "không"); use "không được tán thành" or "bị ban quản lý phản đối".
- "on record" hint = "có ghi trong sử sách" → wrong scale (sử sách = history books); use "đã chính thức tuyên bố" or "công khai cho biết".
- "make a mountain out of a molehill" hint = "Bão tố trong cốc nước" → English calque; use "chuyện bé xé ra to" or "làm to chuyện".
- "go figure" hint = "Có lạ không lạ" → not a real Vietnamese phrase; use "thật khó hiểu" or "ai mà ngờ được".

Other rules:
- Write in natural, colloquial Vietnamese — like a Vietnamese author telling a funny story to friends. A native speaker should feel it was written, not translated.
- When an English idiom is a NOUN or ADJECTIVE describing someone/something (e.g. "Dave, the squeaky wheel"), translate its meaning naturally (e.g. "Dave, tên hay kêu ca") — do NOT cram a proverb into a noun slot.
- Translate meaning and feeling, not word-for-word. Keep the humor and absurdity.
- Avoid stiff calques (e.g. "Theo ý kiến khiêm tốn của tôi") — use the natural form (e.g. "Theo thiển ý của tôi").
- Do not add notes or explanations.

Story:
{text}

Output only the Vietnamese translation, nothing else."""


REVIEW_VIET_PROMPT = """You are auditing a Vietnamese idiom dictionary. The current entry may be excellent, mediocre, or broken. Your job: keep it if good, fix it if bad.

English idiom: "{phrase}"
Meaning: {meaning}
Current Vietnamese: "{viet}"

Run all four checks on "{viet}". REJECT it if ANY check fails:

1. GRAMMAR — Is it a valid Vietnamese phrase? No dangling particles, no broken sentence fragments.
   ✗ BAD: "Bị coi thường không" (trailing "không" makes it a question fragment)
   ✗ BAD: "Có lạ không lạ" (not a real construction)

2. MEANING — Does it match the English idiom's meaning IN GENERAL USAGE? Not just a poetically related image.
   ✗ BAD: "take the plunge" → "Bước vào vành móng ngựa" (= step into courtroom dock — wrong meaning)
   ✗ BAD: "on record" → "có ghi trong sử sách" (= recorded in history books — wrong scale; "on record" means officially stated, not historic)
   ✗ BAD: "curl one's lip" → "Cau mày cau có" (= frowning brow — wrong body part)

3. AUTHENTICITY — Is it a REAL Vietnamese expression, not a word-for-word translation of an English idiom (a "calque")?
   ✗ BAD: "make a mountain out of a molehill" → "Bão tố trong cốc nước" (= calque of "storm in a teacup")
   ✗ BAD: "in my humble opinion" → "Theo ý kiến khiêm tốn của tôi" (literal calque, stiff)
   ✓ GOOD: "in my humble opinion" → "Theo thiển ý của tôi" (real Vietnamese form)

4. NATURALNESS — Would a native Vietnamese speaker actually say this? Not an over-literal description or a clunky calque.

If ALL four checks pass → output "{viet}" unchanged.
If ANY check fails → output a better Vietnamese rendering, in this order of preference:
  a) A real Vietnamese idiom/proverb/saying with the same meaning
  b) A natural Vietnamese colloquial phrase that captures the meaning
  c) A short plain Vietnamese phrase (only as last resort)

OUTPUT FORMAT: exactly one short Vietnamese phrase on a single line. No quotes. No dash. No explanation. No "Vietnamese:" prefix."""


EDITOR_PROMPT = """Bạn là một biên tập viên người Việt đang đọc lại bản dịch của một câu chuyện vui.

Câu chuyện gốc tiếng Anh:
{source}

Bản dịch hiện tại (cần biên tập):
{translation}

Nhiệm vụ của bạn: đọc bản dịch như một độc giả người Việt thực sự, rồi viết lại để câu văn trôi chảy tự nhiên như do một nhà văn người Việt viết — không còn dấu vết của một bản dịch máy móc.

Tập trung sửa các lỗi sau:
1. CALQUE (dịch sát từng chữ từ tiếng Anh): nếu một cụm từ nghe như dịch máy thay vì cách nói thật của người Việt, viết lại bằng cụm tự nhiên. Ví dụ: "Giữ ai trong vòng tối tăm" → "giấu giếm ai" / "giữ kín không cho ai biết".
2. NGỮ PHÁP GƯỢNG: câu cú lủng củng, lặp từ vô lý, hoặc đuôi câu cụt → viết lại trôi chảy.
3. NGHĨA SAI: nếu một cụm bị dùng sai ngữ cảnh (ví dụ "bước vào vành móng ngựa" cho ý nghĩa "đánh liều") → thay bằng cách diễn đạt đúng nghĩa.
4. GIỌNG VĂN: giữ giọng hài hước, châm biếm của bản gốc. Không trang trọng hóa, không thêm chú thích.

Yêu cầu giữ nguyên:
- Tiêu đề (#)
- Tên nhân vật (Marcus, Diane, v.v.)
- Tổng thể cốt truyện và các tình tiết
- Các thành ngữ tiếng Việt thật sự tự nhiên đã có sẵn — đừng thay bằng cách diễn đạt khác

Nếu bản dịch đã thực sự tự nhiên rồi, có thể giữ nguyên hoặc chỉ chỉnh sửa rất nhẹ.

Chỉ xuất bản tiếng Việt đã biên tập, không kèm giải thích, không kèm nhãn "Bản đã sửa:" hay tương tự."""


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
    edit: bool = True,
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
    translation = resp.content[0].text.strip() if resp.content else ""
    if edit and translation:
        translation = edit_vietnamese_story(text, translation, client, model=model)
    return translation


def edit_vietnamese_story(
    source: str,
    translation: str,
    client: Anthropic,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Second-pass editor: smooth out calques and awkward phrasing in a Vietnamese translation."""
    resp = client.messages.create(
        model=model,
        max_tokens=900,
        messages=[{"role": "user", "content": EDITOR_PROMPT.format(source=source, translation=translation)}],
    )
    edited = resp.content[0].text.strip() if resp.content else ""
    return edited or translation


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


def generate_extra_example(phrase: str, meaning: str, client: Anthropic, model: str = "claude-haiku-4-5-20251001") -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=100,
        messages=[{"role": "user", "content": EXTRA_EXAMPLE_PROMPT.format(phrase=phrase, meaning=meaning)}],
    )
    raw = resp.content[0].text.strip() if resp.content else ""
    return next((l.strip() for l in raw.splitlines() if l.strip()), "")


def generate_extra_story(phrase: str, meaning: str, existing_story: str, client: Anthropic, model: str = "claude-sonnet-4-6") -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": EXTRA_STORY_PROMPT.format(phrase=phrase, meaning=meaning, existing_story=existing_story or "none")}],
    )
    return resp.content[0].text.strip() if resp.content else ""


def fill_extra_examples(db_path: str, client: Anthropic, target: int = 5) -> int:
    with db.connect(db_path) as conn:
        rows = db.idioms_needing_examples(conn, target)
    added = 0
    total = len(rows)
    for i, row in enumerate(rows, 1):
        needed = target - row["example_count"]
        for _ in range(needed):
            sentence = generate_extra_example(row["phrase"], row["meaning"], client)
            if sentence:
                with db.connect(db_path) as conn:
                    db.add_example(conn, row["id"], sentence)
                added += 1
        if i % 100 == 0:
            print(f"  {i}/{total} idioms ({added} examples added)...", flush=True)
    return added


def fill_extra_stories(db_path: str, client: Anthropic, target: int = 3) -> int:
    with db.connect(db_path) as conn:
        rows = db.idioms_needing_stories(conn, target)
    added = 0
    total = len(rows)
    for i, row in enumerate(rows, 1):
        needed = target - row["story_count"]
        # Pass the most recent existing story as context so Claude varies the new one
        with db.connect(db_path) as conn:
            existing_row = conn.execute(
                "SELECT story FROM idiom_stories WHERE idiom_id = ? ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
        existing_story = existing_row["story"] if existing_row else ""
        for _ in range(needed):
            story = generate_extra_story(row["phrase"], row["meaning"], existing_story, client)
            if story:
                with db.connect(db_path) as conn:
                    db.add_idiom_story(conn, row["id"], story)
                added += 1
                existing_story = story
        if i % 50 == 0:
            print(f"  {i}/{total} idioms ({added} stories added)...", flush=True)
    return added
