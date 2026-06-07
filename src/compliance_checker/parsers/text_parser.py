"""
parsers/text_parser.py
=======================
Turn free-text standards (e.g. an excerpt of TIA-942) into a structured list of
`Rule` objects.

Strategy:
  * If GEMINI_API_KEY is present -> call Gemini 1.5 Pro asking for strict JSON
    that matches our Pydantic `RuleSet` schema, then validate it.
  * Otherwise (or on any failure) -> fall back to a deterministic regex/keyword
    based mock parser so the pipeline always produces sensible output offline.
"""

from __future__ import annotations

import json
import os
import re
import time

from pydantic import ValidationError

from src.compliance_checker.engine.rules import Condition, Rule, RuleSet, TargetClass


# ---------------------------------------------------------------------------
# Document loading (PDF / TXT)
# ---------------------------------------------------------------------------
def extract_text(path: str) -> str:
    """
    Read a standard document and return its plain text.

    Supports:
      * .pdf  -> extracted page-by-page with `pypdf`
      * .txt / anything else -> read as UTF-8 text

    This is what you point at the PDF you drop into `data/`.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _extract_pdf_text(path)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _extract_pdf_text(path: str) -> str:
    """Extract all text from a PDF using pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Reading PDF standards requires `pypdf`. Install it with: "
            "pip install pypdf"
        ) from exc

    reader = PdfReader(path)
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n".join(pages).strip()
    print(f"[text_parser] Extracted {len(text)} chars from PDF "
          f"({len(reader.pages)} pages): {os.path.basename(path)}")
    return text


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are a building-compliance engineer. From the standard/regulation text
below (e.g. TIA-942, BEAM Plus, Hong Kong Buildings Ordinance Cap.123),
extract ONLY the rules that can be checked against a floor-plan graph whose
entities are listed under "CHECKABLE VOCABULARY". Ignore everything that cannot
be expressed against those entities (administrative procedures, documentation,
fire-rating chemistry, etc.). Return STRICT JSON.

Return ONLY a JSON object of the form:
{
  "rules": [
    {
      "target_class": "Room" | "Aisle" | "Rack" | "Equipment" | "Building" | "Any",
      "target_type": "<token from CHECKABLE VOCABULARY or null>",
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
- target_class "Aisle"  -> "Cold" or "Hot" (data-center hot/cold aisles).
- target_class "Building" -> facility-wide; use condition "max_pue", target_type null.
- target_class "Any" / target_type null -> applies to every space.

CONDITION MEANINGS:
- min_area      : a space's area (m²) must be >= value.
- min_width / max_width : a space/aisle/corridor width (m) bound.
- min_clearance : clear access distance (m) must be >= value.
- min_power     : equipment rated power (kW) must be >= value.
- max_pue       : facility PUE must be <= value (Building only).
- must_exist    : at least one such entity must be present (value repeats the token).
- must_connect_to : the target_type entities must be graph-connected to the
                    token in "value" (e.g. every room must_connect_to "corridor").

Rules:
- Use ONLY the tokens/conditions listed above. If a requirement does not fit,
  DROP it rather than inventing a new token.
- For numeric conditions, convert the value to the stated SI unit (m, m², kW).
- Do not output any prose, markdown, or code fences. JSON only.
- If a chunk contains no checkable rules, return {"rules": []}.

STANDARD TEXT:
"""


# Large standards must be chunked: a single 400+ page PDF would either blow the
# context window or get its JSON answer truncated by the output-token limit.
# We split the text into ~MAX_CHARS_PER_CHUNK windows and parse each separately.
MAX_CHARS_PER_CHUNK = 60_000

# Free-tier Gemini has tight rate limits. We (a) skip chunks that contain no
# rule-like language so we never spend a call on a table-of-contents page, and
# (b) throttle to >= GEMINI_MIN_INTERVAL_S between real calls.
_RULE_SIGNAL = re.compile(
    r"\b(shall|must|minimum|maximum|at least|not less than|not more than|"
    r"no less than|required|provide[d]?|clearance|width|height|area|"
    r"m2|m²|sq\.?\s*m|metres?|meters?|mm|kw|pue|ventilation|exit|escape)\b",
    re.IGNORECASE,
)


def _looks_like_rules(chunk: str) -> bool:
    """Cheap pre-filter: does this chunk plausibly contain checkable rules?"""
    # Require a few signal hits so prose mentioning a keyword once is skipped.
    return len(_RULE_SIGNAL.findall(chunk)) >= 3


def _chunk_text(text: str, size: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    """Split text into <= `size` chunks, preferring paragraph boundaries."""
    text = text.strip()
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Back off to the nearest paragraph/line break for a clean cut.
            brk = text.rfind("\n\n", start, end)
            if brk == -1 or brk <= start:
                brk = text.rfind("\n", start, end)
            if brk > start:
                end = brk
        chunks.append(text[start:end])
        start = end
    return chunks


_MAX_RETRIES = 4  # per chunk, for transient (429/500/503) API errors


def _is_transient(exc: Exception) -> bool:
    """True for retryable API errors (rate limit / server overload)."""
    text = f"{exc.__class__.__name__} {exc}".lower()
    return any(code in text for code in
               ("503", "500", "502", "504", "429", "unavailable",
                "overloaded", "high demand", "resourceexhausted", "deadline"))


def _gemini_extract_chunk(client, model_name: str, chunk: str) -> list[Rule]:
    """
    Send one chunk to Gemini and validate the returned JSON.

    Retries transient server errors (503 high-demand, 429 rate-limit) with
    exponential backoff. Raises on the final failure so the caller can count it.
    """
    # NB: concatenate (don't str.format) — the prompt contains literal JSON braces.
    prompt = _SYSTEM_PROMPT + "\n\"\"\"\n" + chunk + "\n\"\"\"\n"

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            raw = (response.text or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
            if not raw:
                return []
            return RuleSet.model_validate(json.loads(raw)).rules
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if _is_transient(exc) and attempt < _MAX_RETRIES:
                wait = 2 ** attempt   # 2, 4, 8 s
                print(f"[text_parser]     transient error "
                      f"({exc.__class__.__name__}), retry {attempt}/{_MAX_RETRIES - 1} "
                      f"in {wait}s")
                time.sleep(wait)
                continue
            raise
    raise last_exc  # pragma: no cover


def _parse_with_gemini(text: str, api_key: str) -> tuple[list[Rule], int]:
    """
    Call Gemini (new `google-genai` SDK) over one or more chunks.

    Returns (rules, n_failed_chunks). A failed chunk (after retries) is skipped
    so partial extraction survives; the caller uses n_failed_chunks to decide
    whether the result is complete enough to cache.
    """
    from google import genai  # lazy import so offline mode needs no SDK

    client = genai.Client(api_key=api_key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    min_interval = float(os.getenv("GEMINI_MIN_INTERVAL_S", "4"))

    all_chunks = _chunk_text(text)
    # Pre-filter: only spend API calls on chunks with rule-like language.
    todo = [(i, c) for i, c in enumerate(all_chunks, start=1) if _looks_like_rules(c)]
    skipped = len(all_chunks) - len(todo)
    print(f"[text_parser] {len(all_chunks)} chunk(s); "
          f"{len(todo)} look rule-bearing, {skipped} skipped (no API call).")

    rules: list[Rule] = []
    failed = 0
    last_call = 0.0
    for n, (i, chunk) in enumerate(todo, start=1):
        # Throttle to respect the free-tier requests-per-minute limit.
        gap = min_interval - (time.monotonic() - last_call)
        if gap > 0:
            time.sleep(gap)
        try:
            chunk_rules = _gemini_extract_chunk(client, model_name, chunk)
            rules.extend(chunk_rules)
            print(f"[text_parser]   [{n}/{len(todo)}] chunk {i}: +{len(chunk_rules)} rules")
        except Exception as exc:  # noqa: BLE001 - count & continue
            failed += 1
            print(f"[text_parser]   [{n}/{len(todo)}] chunk {i} FAILED "
                  f"({exc.__class__.__name__}: {str(exc)[:80]}).")
        last_call = time.monotonic()

    print(f"[text_parser] Gemini ({model_name}) extracted {len(rules)} rules; "
          f"{failed} chunk(s) failed of {len(todo)} attempted.")
    return rules, failed


# ---------------------------------------------------------------------------
# Mock parser (offline fallback)
# ---------------------------------------------------------------------------
# Each pattern maps a regex over the lowered text to a Rule factory. This is a
# genuine keyword extractor, not a stub: it reads numbers out of the sentence.
_NUM = r"([0-9]+(?:\.[0-9]+)?)"


def _mock_parse(text: str) -> list[Rule]:
    """
    Deterministic keyword/regex parser used when no API key is available.

    It recognises the patterns present in data/sample_standard.txt and is a
    reasonable approximation of what the LLM would return.
    """
    rules: list[Rule] = []
    # Collapse all whitespace (incl. line wraps) so sentence-level regexes
    # don't break across newlines.
    lowered = re.sub(r"\s+", " ", text.lower())

    def add(rule: Rule) -> None:
        rules.append(rule)

    # --- Cold aisle minimum width ---
    m = re.search(rf"cold aisle[s]?[^.]*?(?:at least|minimum|min)[^.]*?{_NUM}\s*m", lowered)
    if m:
        add(Rule(target_class=TargetClass.AISLE, target_type="Cold",
                 condition=Condition.MIN_WIDTH, value=float(m.group(1)), unit="m",
                 description=f"Cold aisles must be at least {m.group(1)} m wide.",
                 source="TIA-942 §5.1"))

    # --- Hot aisle minimum width ---
    m = re.search(rf"hot aisle[s]?[^.]*?(?:at least|minimum|min)[^.]*?{_NUM}\s*m", lowered)
    if m:
        add(Rule(target_class=TargetClass.AISLE, target_type="Hot",
                 condition=Condition.MIN_WIDTH, value=float(m.group(1)), unit="m",
                 description=f"Hot aisles must be at least {m.group(1)} m wide.",
                 source="TIA-942 §5.1"))

    # --- data_hall minimum area ---
    m = re.search(rf"data[_ ]hall[^.]*?(?:at least|minimum|min)[^.]*?{_NUM}\s*(?:m2|m²|sq)", lowered)
    if m:
        add(Rule(target_class=TargetClass.ROOM, target_type="data_hall",
                 condition=Condition.MIN_AREA, value=float(m.group(1)), unit="m2",
                 description=f"Each data_hall must be at least {m.group(1)} m² in area.",
                 source="TIA-942 §5.2"))

    # --- electrical_room must exist ---
    if re.search(r"electrical[_ ]room[^.]*(must exist|at least one)", lowered):
        add(Rule(target_class=TargetClass.ROOM, target_type="electrical_room",
                 condition=Condition.MUST_EXIST, value="electrical_room",
                 description="At least one electrical_room must exist.",
                 source="TIA-942 §5.3"))

    # --- CRAC cooling must exist ---
    if re.search(r"\bcrac\b", lowered):
        add(Rule(target_class=TargetClass.EQUIPMENT, target_type="crac",
                 condition=Condition.MUST_EXIST, value="crac",
                 description="At least one CRAC unit must exist.",
                 source="TIA-942 §5.4"))

    # --- UPS must exist ---
    if re.search(r"\bups\b", lowered):
        add(Rule(target_class=TargetClass.EQUIPMENT, target_type="ups",
                 condition=Condition.MUST_EXIST, value="ups",
                 description="At least one UPS must exist.",
                 source="TIA-942 §5.5"))

    # --- Connectivity: data_hall must connect to electrical_room ---
    if re.search(r"data[_ ]hall[^.]*connect[^.]*electrical[_ ]room", lowered):
        add(Rule(target_class=TargetClass.ROOM, target_type="data_hall",
                 condition=Condition.MUST_CONNECT_TO, value="electrical_room",
                 description="Every data_hall must connect to an electrical_room.",
                 source="TIA-942 §5.6"))

    # --- PUE ceiling ---
    m = re.search(rf"pue[^.]*?(?:not exceed|<=|below|under|max(?:imum)?)[^.]*?{_NUM}", lowered)
    if m:
        add(Rule(target_class=TargetClass.BUILDING, target_type=None,
                 condition=Condition.MAX_PUE, value=float(m.group(1)),
                 description=f"Facility PUE must not exceed {m.group(1)}.",
                 source="TIA-942 §5.7"))

    print(f"[text_parser] Mock parser extracted {len(rules)} rules.")
    return rules


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def parse_standard_text(text: str) -> tuple[list[Rule], dict]:
    """
    Parse a text standard into Rules.

    Returns (rules, info) where info = {
        "engine": "gemini" | "mock",
        "complete": bool,   # True only if Gemini ran with zero failed chunks
    }.
    The caller (main) caches a document's rules ONLY when complete, so a
    transient API error or the mock fallback never poisons the cache.

    Uses Gemini when GEMINI_API_KEY is set; otherwise the offline mock parser.
    If the Gemini SDK/client itself blows up (not a per-chunk error) we degrade
    to the mock parser but mark the result incomplete.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            rules, failed = _parse_with_gemini(text, api_key)
            return rules, {"engine": "gemini", "complete": failed == 0}
        except Exception as exc:  # noqa: BLE001 - SDK/client-level failure
            print(f"[text_parser] Gemini path failed ({exc.__class__.__name__}: "
                  f"{str(exc)[:120]}); using mock parser.")
    else:
        print("[text_parser] No GEMINI_API_KEY found; using offline mock parser.")
    return _mock_parse(text), {"engine": "mock", "complete": False}
