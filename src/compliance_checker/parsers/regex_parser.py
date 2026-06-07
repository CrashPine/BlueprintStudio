"""
parsers/regex_parser.py
=======================
Offline rule extractor: turns a plain-text / PDF standard into machine-checkable
`Rule` objects using spaCy sentence segmentation + deterministic regex/keyword
matching. No external LLM is required (works fully offline).

It understands the four rule families the validator supports:
  * Numeric    — min/max width, min area, min power, max PUE (PUE may be unitless)
  * must_exist — "at least one CRAC ... must exist"
  * must_connect_to — "every data_hall must connect to an electrical_room"

Vocabulary matching is underscore/space/acronym aware so the standard can be
written in the unified-graph tokens (e.g. `data_hall`, `electrical_room`) or in
prose ("data hall", "CRAC").
"""

from __future__ import annotations

import os
import re

from src.compliance_checker.engine.rules import (
    AISLE_TYPES,
    Condition,
    EQUIPMENT_TYPES,
    ROOM_CATEGORIES,
    Rule,
    TargetClass,
    is_rule_in_vocabulary,
)

try:
    import spacy

    try:
        _nlp = spacy.load("en_core_web_sm")
    except OSError:
        import subprocess

        print("Downloading spacy model en_core_web_sm...")
        subprocess.run(["python", "-m", "spacy", "download", "en_core_web_sm"], check=True)
        _nlp = spacy.load("en_core_web_sm")
except Exception:  # noqa: BLE001 - spaCy optional; fall back to regex split
    _nlp = None

# Tokens that may appear as both a room category and equipment type, etc.
_NON_AISLE_TOKENS = ROOM_CATEGORIES | EQUIPMENT_TYPES


def _sentences(text: str) -> list[str]:
    if _nlp is not None:
        return [s.text.strip() for s in _nlp(text).sents if s.text.strip()]
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}", text)
    return [p.strip() for p in parts if p.strip()]


_SECTION_HEADER = re.compile(r"(?m)^\s*\d+(?:\.\d+)+\s+.*$")


def _clauses(text: str) -> list[str]:
    """Split a numbered standard into clause bodies.

    Splits on section headers like '5.1 Aisles ...', drops the heading line
    (and any preamble before the first section) so enumerators and vocabulary
    lists don't pollute number/target extraction. Each clause body is then
    sentence-segmented. Falls back to plain sentences when no headers exist.
    """
    headers = list(_SECTION_HEADER.finditer(text))
    if not headers:
        return _sentences(text)

    sents: list[str] = []
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end].strip()
        if body:
            sents.extend(_sentences(body))
    return sents


def _find_tokens(text_l: str, tokens: set[str]) -> list[tuple[int, str]]:
    """Return [(position, canonical_token)] for each vocab token present.

    Matches the canonical underscore form, its spaced form, and (for short
    tokens) a whole-word acronym (e.g. 'crac', 'ups')."""
    hits: list[tuple[int, str]] = []
    for tok in tokens:
        variants = {tok, tok.replace("_", " ")}
        best: int | None = None
        for v in variants:
            idx = text_l.find(v)
            if idx != -1 and (best is None or idx < best):
                best = idx
        if best is not None:
            hits.append((best, tok))
    hits.sort()
    return hits


def _detect_target(s: str) -> tuple[TargetClass, str | None]:
    """Classify the sentence's primary target (class, type)."""
    # Aisles win when the sentence is explicitly about aisles.
    if "aisle" in s:
        for a in AISLE_TYPES:
            if a in s:
                return TargetClass.AISLE, a
        return TargetClass.AISLE, None

    # Building-level metrics (PUE / facility / building).
    if "pue" in s or "facility" in s or "building" in s:
        return TargetClass.BUILDING, "Building"

    hits = _find_tokens(s, _NON_AISLE_TOKENS)
    if hits:
        tok = hits[0][1]
        if tok in ROOM_CATEGORIES:
            return TargetClass.ROOM, tok
        if tok == "rack":
            return TargetClass.RACK, "rack"
        return TargetClass.EQUIPMENT, tok
    return TargetClass.ANY, None


_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"(mm|cm|m2|sqft|sq\.?\s*m|m|ft|in|kw|w)?"


def _extract_number(
    s: str, require_unit: bool, prefer_after: str | None = None
) -> tuple[float | None, str | None]:
    """Pick a numeric value from the sentence, scanning all candidates.

    require_unit=True returns the first number that carries a unit (skips bare
    section/enumerator numbers). For unitless metrics (PUE) we prefer the number
    following `prefer_after` (e.g. 'exceed'), else the last number."""
    cands: list[tuple[int, float, str | None]] = []
    for m in re.finditer(_NUM + r"\s*" + _UNIT, s):
        unit = (m.group(2) or "").strip() or None
        cands.append((m.start(), float(m.group(1)), unit))
    if not cands:
        return None, None

    if require_unit:
        for _pos, val, unit in cands:
            if unit:
                return val, unit
        return None, None

    if prefer_after:
        kpos = s.find(prefer_after)
        if kpos != -1:
            after = [c for c in cands if c[0] > kpos]
            if after:
                return after[0][1], after[0][2]
    # No keyword anchor: take the last number (avoids a leading section index).
    return cands[-1][1], cands[-1][2]


def _existence_rule(s: str, sent: str) -> Rule | None:
    """Match 'at least one X must exist' / 'X must be present'."""
    if not re.search(r"(must|shall)\s+(exist|be\s+present)|at least one", s):
        return None
    anchor = s.find("exist")
    if anchor == -1:
        anchor = s.find("present")
    if anchor == -1:
        anchor = len(s)
    hits = _find_tokens(s, _NON_AISLE_TOKENS)
    if not hits:
        return None
    # The subject of "... must exist" is the token before the anchor; prefer the
    # nearest one before it, falling back to the nearest overall.
    before = [h for h in hits if h[0] <= anchor]
    pos, tok = (before[-1] if before else min(hits, key=lambda h: abs(h[0] - anchor)))
    if tok in ROOM_CATEGORIES:
        tc = TargetClass.ROOM
    elif tok == "rack":
        tc = TargetClass.RACK
    else:
        tc = TargetClass.EQUIPMENT
    return Rule(
        target_class=tc,
        target_type=tok,
        condition=Condition.MUST_EXIST,
        value=tok,
        description=sent.strip(),
        source="Regex Parser",
    )


def _connection_rule(s: str, sent: str) -> Rule | None:
    """Match 'X must connect to Y [through ...]'."""
    if "connect" not in s:
        return None
    parts = re.split(r"connect(?:ed|s)?\s+to", s, maxsplit=1)
    if len(parts) != 2:
        return None
    src_hits = _find_tokens(parts[0], _NON_AISLE_TOKENS)
    tgt_hits = _find_tokens(parts[1], _NON_AISLE_TOKENS)
    if not src_hits or not tgt_hits:
        return None
    src = src_hits[0][1]
    tgt = tgt_hits[0][1]
    tc = TargetClass.ROOM if src in ROOM_CATEGORIES else TargetClass.EQUIPMENT
    return Rule(
        target_class=tc,
        target_type=src,
        condition=Condition.MUST_CONNECT_TO,
        value=tgt,
        description=sent.strip(),
        source="Regex Parser",
    )


def _numeric_rule(s: str, sent: str) -> Rule | None:
    target_class, target_type = _detect_target(s)

    # Determine condition (check PUE before 'power' to avoid mis-classifying
    # 'Power Usage Effectiveness').
    condition: Condition | None = None
    require_unit = True
    prefer_after: str | None = None
    if "pue" in s:
        condition = Condition.MAX_PUE
        target_class, target_type = TargetClass.BUILDING, "Building"
        require_unit = False
        prefer_after = "exceed" if "exceed" in s else "pue"
    elif "width" in s or "wide" in s:
        condition = (
            Condition.MAX_WIDTH if ("max" in s or "exceed" in s) else Condition.MIN_WIDTH
        )
    elif "clearance" in s:
        condition = Condition.MIN_CLEARANCE
    elif "area" in s:
        condition = Condition.MIN_AREA
    elif "power" in s or "kw" in s:
        condition = Condition.MIN_POWER

    if condition is None:
        return None

    val, unit = _extract_number(s, require_unit=require_unit, prefer_after=prefer_after)
    if val is None:
        return None

    return Rule(
        target_class=target_class,
        target_type=target_type,
        condition=condition,
        value=val,
        unit=unit,
        description=sent.strip(),
        source="Regex Parser",
    )


def _rules_from_sentence(sent: str) -> list[Rule]:
    s = sent.lower()
    out: list[Rule] = []

    conn = _connection_rule(s, sent)
    if conn is not None:
        out.append(conn)
        return out

    exist = _existence_rule(s, sent)
    if exist is not None:
        out.append(exist)
        return out

    numeric = _numeric_rule(s, sent)
    if numeric is not None:
        out.append(numeric)
    return out


def parse_document(path: str) -> tuple[list[Rule], dict]:
    """Extract rules from a PDF or text standard (offline)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    else:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()

    rules: list[Rule] = []
    for sent in _clauses(text):
        rules.extend(_rules_from_sentence(sent))

    rules = [r for r in rules if is_rule_in_vocabulary(r)]
    print(f"[regex_parser] Extracted {len(rules)} rules from {os.path.basename(path)}")
    return rules, {"engine": "regex", "complete": True}
