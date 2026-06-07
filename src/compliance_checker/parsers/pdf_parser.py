"""
parsers/pdf_parser.py
=====================
Extract compliance Rules from PDF/text standards using a LOCAL LLM via Ollama
(default model `mistral`). This replaces the Gemini-based text parser.

Why local:
  * No API quota / cost — the previous Gemini free tier capped us at ~20
    requests/day, which is unusable for 400+ page codes.
  * Runs fully offline against a local Ollama server.

Pipeline:
  1. `pdfplumber` opens the PDF and we read it page by page.
  2. Pages are grouped into small chunks (default 3 pages) so each prompt stays
     well within the local model's context window.
  3. A cheap keyword pre-filter drops chunks with no rule-like language so we
     don't spend model time on tables of contents / cover pages.
  4. Each chunk goes to `ollama.chat(model=..., format='json', messages=[...])`
     with a system prompt that pins the output to our Pydantic Rule schema.
  5. Returned JSON is validated through Pydantic; rules from all chunks are
     aggregated and returned.

Public entry point: `parse_document(path) -> (list[Rule], info_dict)`.
The info dict ({"engine": "ollama", "complete": bool}) lets main.py decide
whether the extraction is complete enough to cache.
"""

from __future__ import annotations

import json
import os
import re
from dotenv import load_dotenv
from pydantic import ValidationError


from src.compliance_checker.engine.rules import Rule, is_rule_in_vocabulary


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, "..", ".env"))

PAGES_PER_CHUNK = int(os.getenv("PDF_PAGES_PER_CHUNK", "3"))
# Fallback char-window when chunking a plain-text (non-PDF) document.
TEXT_CHARS_PER_CHUNK = int(os.getenv("PDF_TEXT_CHARS_PER_CHUNK", "8000"))


# ---------------------------------------------------------------------------
# System prompt — pins Ollama output to our Pydantic Rule schema
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are a building-compliance engineer. From the standard/regulation text the
user provides (e.g. TIA-942, BEAM Plus, Hong Kong Buildings Ordinance Cap.123),
extract ONLY the rules that can be checked against a floor-plan graph whose
entities are listed in CHECKABLE VOCABULARY. Ignore anything that cannot be
expressed against those entities (administrative procedures, documentation,
fire-rating chemistry, etc.).

Respond with a SINGLE JSON object, no prose, of exactly this shape:
{
  "rules": [
    {
      "target_class": "Room" | "Aisle" | "Rack" | "Equipment" | "Building" | "Any",
      "target_type": "<token from CHECKABLE VOCABULARY, or null>",
      "condition": "min_width" | "max_width" | "min_area" | "min_clearance"
                   | "min_power" | "max_pue" | "must_connect_to" | "must_exist",
      "value": <number for numeric conditions, or a target_type token for must_connect_to>,
      "unit": "<unit or null>",
      "description": "<one-line restatement>",
      "source": "<clause / section citation if present, else null>"
    }
  ]
}

CHECKABLE VOCABULARY (target_type MUST be one of these exact tokens):
- target_class "Room"  -> space category:
      "data_hall", "electrical_room", "plant_room", "office", "corridor".
  (Map synonyms: server/computer room -> data_hall; switchroom/transformer room
   -> electrical_room; mechanical/chiller/AHU room -> plant_room; passage/
   hallway/escape route -> corridor.)
- target_class "Equipment" -> node type:
      "chiller", "pump", "cooling_tower", "ahu", "fcu", "boiler", "fan",
      "transformer", "switchgear", "breaker", "distribution_panel", "meter",
      "crac", "crah", "ups", "pdu", "busway".
- target_class "Aisle"  -> "Cold" or "Hot".
- target_class "Building" -> facility-wide; use condition "max_pue", target_type null.
- target_class "Any" / target_type null -> applies to every space.

CONDITION MEANINGS:
- min_area      : a space's area (m2) must be >= value.
- min_width / max_width : a space/aisle/corridor width (m) bound.
- min_clearance : clear access distance (m) must be >= value.
- min_power     : equipment rated power (kW) must be >= value.
- max_pue       : facility PUE must be <= value (Building only).
- must_exist    : at least one such entity must be present (value repeats the token).
- must_connect_to : the target_type entities must be connected to the token in "value".

RULES:
- Use ONLY the tokens/conditions listed above. If a requirement does not fit,
  DROP it rather than inventing a token.
- Convert numeric values to SI units (m, m2, kW).
- If the text contains no checkable rules, return {"rules": []}.
"""


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
_RULE_SIGNAL = re.compile(
    r"\b(shall|must|minimum|maximum|at least|not less than|not more than|"
    r"no less than|required|provide[d]?|clearance|width|height|area|"
    r"m2|m²|sq\.?\s*m|metres?|meters?|mm|kw|pue|ventilation|exit|escape)\b",
    re.IGNORECASE,
)


def _looks_like_rules(chunk: str) -> bool:
    """Cheap pre-filter: does this chunk plausibly contain checkable rules?"""
    return len(_RULE_SIGNAL.findall(chunk)) >= 3


def _pdf_chunks(path: str, pages_per_chunk: int = PAGES_PER_CHUNK) -> list[str]:
    """Read a PDF with pdfplumber and group its pages into text chunks."""
    import pdfplumber

    chunks: list[str] = []
    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for start in range(0, n_pages, pages_per_chunk):
            block = pdf.pages[start:start + pages_per_chunk]
            text = "\n".join((page.extract_text() or "") for page in block).strip()
            if text:
                chunks.append(text)
    print(f"[pdf_parser] {os.path.basename(path)}: {n_pages} pages "
          f"-> {len(chunks)} chunk(s) of up to {pages_per_chunk} pages.")
    return chunks


def _text_chunks(path: str, size: int = TEXT_CHARS_PER_CHUNK) -> list[str]:
    """Chunk a plain-text document by characters (for the .txt sample)."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            brk = text.rfind("\n", start, end)
            if brk > start:
                end = brk
        chunks.append(text[start:end])
        start = end
    return chunks


# ---------------------------------------------------------------------------
# Ollama call + JSON validation
# ---------------------------------------------------------------------------
def _coerce_ruleset(data) -> list[Rule]:
    """
    Validate the model's JSON into a list[Rule], tolerating shape variations:
      * {"rules": [...]}        (preferred)
      * [ {...}, {...} ]        (bare list)
      * {...single rule...}     (one object)

    Each candidate is validated INDIVIDUALLY: a single malformed rule (a local
    model often emits one) is skipped instead of discarding the whole chunk.
    """
    if isinstance(data, dict) and isinstance(data.get("rules"), list):
        items = data["rules"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = [data]            # a single rule object
    else:
        items = []

    rules: list[Rule] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            rules.append(Rule.model_validate(item))
        except ValidationError:
            continue              # drop just this malformed rule
    return rules


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _extract_rules_from_chunk(chunk: str, model: str) -> list[Rule]:
    """Send one chunk to Claude API and return validated Rules."""
    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": chunk}],
    )

    content = ""
    for block in response.content:
        if block.type == "text":
            content = block.text
            break
    content = _THINK_RE.sub("", content or "").strip()
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
    content = re.sub(r"```\s*$", "", content, flags=re.MULTILINE).strip()
    if not content:
        return []
    return _coerce_ruleset(json.loads(content))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def parse_document(path: str, model: str | None = None) -> tuple[list[Rule], dict]:
    """
    Parse a PDF (or plain-text) standard into Rules using a local Ollama model.

    Returns (rules, info) where info = {"engine": "ollama", "complete": bool}.
    `complete` is True only if at least one chunk was attempted and none failed,
    so a stopped Ollama server / model error never gets cached by the caller.
    """
    model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    ext = os.path.splitext(path)[1].lower()
    all_chunks = _pdf_chunks(path) if ext == ".pdf" else _text_chunks(path)

    todo = [c for c in all_chunks if _looks_like_rules(c)]
    skipped = len(all_chunks) - len(todo)
    print(f"[pdf_parser] {len(todo)} rule-bearing chunk(s), "
          f"{skipped} skipped; model='{model}'.")

    rules: list[Rule] = []
    failed = 0
    for i, chunk in enumerate(todo, start=1):
        try:
            chunk_rules = _extract_rules_from_chunk(chunk, model)
            rules.extend(chunk_rules)
            print(f"[pdf_parser]   chunk {i}/{len(todo)}: +{len(chunk_rules)} rules")
        except (ValidationError, json.JSONDecodeError) as exc:
            # Bad JSON / schema from the model: skip this chunk, keep going.
            failed += 1
            print(f"[pdf_parser]   chunk {i}/{len(todo)} bad output "
                  f"({exc.__class__.__name__}).")
        except Exception as exc:  # noqa: BLE001 - server down, model missing, etc.
            failed += 1
            print(f"[pdf_parser]   chunk {i}/{len(todo)} FAILED "
                  f"({exc.__class__.__name__}: {str(exc)[:100]}).")

    # Post-validation: drop hallucinated / off-vocabulary rules the local model
    # may have produced (e.g. invented target_types). This is purely a quality
    # filter — every kept rule references a graph-checkable entity.
    raw_count = len(rules)
    rules = [r for r in rules if is_rule_in_vocabulary(r)]
    dropped = raw_count - len(rules)

    complete = bool(todo) and failed == 0
    print(f"[pdf_parser] Extracted {raw_count} rules from {len(todo)} chunk(s); "
          f"{failed} failed; dropped {dropped} off-vocabulary; "
          f"{len(rules)} kept; complete={complete}.")
    return rules, {"engine": "anthropic", "complete": complete}


# Backwards-friendly alias matching the requested name.
def parse_pdf(path: str, model: str | None = None) -> tuple[list[Rule], dict]:
    """Alias of `parse_document` for PDF inputs."""
    return parse_document(path, model=model)
