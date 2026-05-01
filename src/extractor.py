import json
import re
from pathlib import Path

import pdfplumber
from anthropic import Anthropic

from . import db


EXTRACT_PROMPT = """You are extracting English idioms from teaching slides.

From the text below, return a JSON array of objects with keys:
- "phrase": the idiom (lowercase, canonical form, e.g. "break the ice")
- "meaning": short plain-English meaning (one sentence)
- "example": an example sentence from the slide if present, else null

Only include real idioms / fixed expressions. Skip plain vocabulary, headers, page numbers, exercise instructions. Deduplicate.

Return ONLY the JSON array, no prose, no markdown fences.

SLIDE TEXT:
---
{text}
---"""


def read_pdf_text(pdf_path: Path) -> str:
    chunks = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                chunks.append(t)
    return "\n\n".join(chunks)


def _chunk(text: str, size: int = 12000) -> list[str]:
    paras = text.split("\n\n")
    out, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 2 > size and buf:
            out.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(buf)
    return out


def _parse_json_array(raw: str) -> list[dict]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []


def extract_idioms(text: str, client: Anthropic, model: str = "claude-sonnet-4-6") -> list[dict]:
    found: list[dict] = []
    for chunk in _chunk(text):
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=chunk)}],
        )
        raw = resp.content[0].text if resp.content else ""
        found.extend(_parse_json_array(raw))
    seen, deduped = set(), []
    for item in found:
        phrase = (item.get("phrase") or "").strip().lower()
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        deduped.append(
            {
                "phrase": phrase,
                "meaning": (item.get("meaning") or "").strip(),
                "example": (item.get("example") or None),
            }
        )
    return deduped


def ingest_pdf(pdf_path: Path, db_path: str, client: Anthropic) -> tuple[int, int]:
    text = read_pdf_text(pdf_path)
    idioms = extract_idioms(text, client)
    added = 0
    with db.connect(db_path) as conn:
        for it in idioms:
            if not it["meaning"]:
                continue
            new_id = db.add_idiom(conn, it["phrase"], it["meaning"], it["example"], pdf_path.name)
            if new_id is not None:
                added += 1
    return added, len(idioms)
