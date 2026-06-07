"""
main.py
=======
End-to-end orchestrator for the Data Center Compliance Checker.

Workflow:
    1. Load the standard PDF(s) and parse them into Rules (local Ollama LLM).
    2. Persist the Rules into the database (Postgres, or SQLite fallback).
    3. Locate the floor-plan graph JSON (or generate a sample one).
    4. Parse the graph into the standardized Geometry Model.
    5. Run the validation engine.
    6. Print a human-readable Violations Report.

Run:
    cd dc_compliance_checker
    pip install -r requirements.txt
    python main.py
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys

from dotenv import load_dotenv

from src.compliance_checker.database.setup import init_db, load_rules, save_rules
from src.compliance_checker.engine.rules import Rule, MAX_CONDITIONS
from src.compliance_checker.engine.validator import validate
from src.compliance_checker.parsers.graph_parser import parse_graph_json
from src.compliance_checker.parsers.regex_parser import parse_document as parse_regex
from src.compliance_checker.parsers.structured_parser import parse_structured

import httpx
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
CACHE_DIR = os.path.join(DATA_DIR, ".rules_cache")
SAMPLE_TXT = os.path.join(DATA_DIR, "sample_standard.txt")
SAMPLE_GRAPH = os.path.join(DATA_DIR, "sample_graph.json")


def find_standard_documents() -> list[str]:
    """
    Decide which standard document(s) to parse.

    Priority:
      1. ALL *.pdf files you dropped into `data/` (sorted alphabetically)
      2. ALL *.txt files
      3. ALL *standard*.json files (structured)
    """
    files = []
    files.extend(sorted(glob.glob(os.path.join(DATA_DIR, "*.pdf"))))
    files.extend(sorted(glob.glob(os.path.join(DATA_DIR, "*.txt"))))
    files.extend(sorted(glob.glob(os.path.join(DATA_DIR, "*standard*.json"))))
    if files:
        return files
    return [SAMPLE_TXT]


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Get embeddings from local Ollama for semantic deduplication."""
    if not texts:
        return []
    embeddings = []
    try:
        response = httpx.post(
            "http://localhost:11434/api/embed",
            json={"model": "nomic-embed-text", "input": texts},
            timeout=10.0
        )
        if response.status_code == 200:
            embeddings = response.json().get("embeddings", [])
    except Exception as e:
        print(f"[dedupe] Warning: could not get embeddings from Ollama: {e}")
    
    if not embeddings or len(embeddings) != len(texts):
        return [[0.0]*768 for _ in texts]
    return embeddings

def dedupe_rules(rules: list[Rule]) -> list[Rule]:
    """
    Deduplicate rules via Semantic Deduplication, Unit Normalization, 
    and Stringency Resolution.
    """
    # 1. Unit Normalization
    for r in rules:
        r.normalize_units()

    unique_rules = []
    if not rules:
        return unique_rules
        
    descriptions = [r.description or "" for r in rules]
    embeddings = get_embeddings(descriptions)
    
    merged_indices = set()
    
    for i, r in enumerate(rules):
        if i in merged_indices:
            continue
            
        group = [r]
        merged_indices.add(i)
        
        # Find semantic duplicates
        for j, other_r in enumerate(rules):
            if j in merged_indices:
                continue
                
            same_target = (r.target_class == other_r.target_class and 
                           r.target_type == other_r.target_type and 
                           r.condition == other_r.condition)
                           
            is_similar = False
            if np.any(embeddings[i]) and np.any(embeddings[j]):
                sim = cosine_similarity([embeddings[i]], [embeddings[j]])[0][0]
                if sim > 0.90:
                    is_similar = True
            
            if same_target or is_similar:
                if r.condition == other_r.condition:
                    group.append(other_r)
                    merged_indices.add(j)
                    
        # 3. Stringency Resolution
        best_rule = group[0]
        if len(group) > 1 and r.is_numeric:
            valid_vals = [g_r for g_r in group if isinstance(g_r.value, (int, float))]
            if valid_vals:
                if r.condition in MAX_CONDITIONS:
                    best_rule = min(valid_vals, key=lambda x: float(x.value))
                else:
                    best_rule = max(valid_vals, key=lambda x: float(x.value))
                    
        unique_rules.append(best_rule)
        
    return unique_rules


# ---------------------------------------------------------------------------
# Per-file rule cache (so big PDFs are parsed by Gemini only once)
# ---------------------------------------------------------------------------
def _file_hash(path: str) -> str:
    """SHA-1 of a file's contents — the cache key."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def get_rules_for_document(path: str, reparse: bool = False) -> list[Rule]:
    """
    Return the Rules for one standard document, using a content-addressed cache.

    The first time a file is seen we extract its text and call the parser
    (Gemini); the result is cached at data/.rules_cache/<sha1>.json. Subsequent
    runs read the cache instantly — pass `reparse=True` (CLI `--reparse`) to
    force re-extraction (e.g. after changing the prompt).
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{_file_hash(path)}.json")

    if not reparse and os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        rules = [Rule.model_validate(r) for r in cached]
        print(f"[main] Loaded {len(rules)} cached rules for "
              f"{os.path.basename(path)} (use --reparse to refresh).")
        return rules

    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        rules, info = parse_structured(path)
    else:
        rules, info = parse_regex(path)

    # Only cache a COMPLETE extraction. A failed run (Ollama down / model error
    # / bad JSON on a chunk) must not poison the cache.
    if info.get("complete"):
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump([r.model_dump() for r in rules], fh, indent=2)
        print(f"[main] Cached {len(rules)} rules for {os.path.basename(path)}.")
    else:
        print(f"[main] NOT caching {os.path.basename(path)} "
              f"(engine={info.get('engine')}, incomplete) — will retry next run.")
    return rules


# ---------------------------------------------------------------------------
# Floor-plan graph discovery
# ---------------------------------------------------------------------------
def find_graph_document() -> str | None:
    """
    Find the floor-plan graph to validate.

    Looks for any *.json in `data/` that looks like a unified graph (has
    'nodes' and 'edges'). Returns the first match, or None if none found.
    """
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, dict) and "nodes" in doc and "edges" in doc:
                return path
        except (json.JSONDecodeError, OSError):
            continue
    return None


# ---------------------------------------------------------------------------
# Sample graph generator (so the PoC is self-contained)
# ---------------------------------------------------------------------------
def generate_sample_graph(path: str) -> None:
    """
    Write a small unified-graph JSON with deliberate violations.

    Physical scale: image 1000x800 px maps to a 40 m x 32 m building, so
    Sx=40, Sy=32 m per normalized unit (the parser recovers this from area_m2).

      Spaces:
        * data_hall      288 m2  -> PASSES "data_hall min_area >= 20"
        * electrical_room 76.8 m2

      Racks: 3 rows of 3 racks with 1.0 m gaps -> two derived aisles:
        * aisle-01 (Cold) 1.0 m  -> VIOLATES "Cold aisle min_width >= 1.2"
        * aisle-02 (Hot)  1.0 m  -> PASSES   "Hot aisle min_width >= 0.9"

      Equipment:
        * crac in data_hall            -> "CRAC must exist" PASSES
        * pdu  in electrical_room      -> wired to a data_hall rack, so
                                          "data_hall must connect to
                                          electrical_room" PASSES
        * (NO ups present)             -> VIOLATES "UPS must exist"

      Meta: pue = 1.7                  -> VIOLATES "PUE <= 1.5"
    """
    SX, SY = 40.0, 32.0  # metres per normalized unit on each axis

    def nx_(x_m: float) -> float:
        return round(x_m / SX, 6)

    def ny_(y_m: float) -> float:
        return round(y_m / SY, 6)

    def rect_poly(x0, y0, x1, y1):
        return [[nx_(x0), ny_(y0)], [nx_(x1), ny_(y0)],
                [nx_(x1), ny_(y1)], [nx_(x0), ny_(y1)]]

    def bbox(x0, y0, x1, y1):
        return [nx_(x0), ny_(y0), nx_(x1), ny_(y1)]

    spaces = [
        {"id": "data_hall_1", "name": "Data Hall 1", "category": "data_hall",
         "polygon_2d": rect_poly(2, 3.2, 20, 19.2), "area_m2": 288.0,
         "it_power_kW": 200.0, "confidence": 0.95},
        {"id": "elec_1", "name": "Electrical Room", "category": "electrical_room",
         "polygon_2d": rect_poly(22, 3.2, 30, 12.8), "area_m2": 76.8,
         "confidence": 0.92},
    ]

    # 3 rows x 3 racks; rack 0.6 m (x) by 1.0 m (y); rows at y=4,6,8 (gaps 1.0 m)
    nodes = []
    rack_ids = []
    for r, y0 in enumerate([4.0, 6.0, 8.0]):
        for c in range(3):
            x0 = 3.0 + c * 1.0
            rid = f"rack_{r}_{c}"
            rack_ids.append(rid)
            nodes.append({
                "id": rid, "type": "rack", "tag": f"R{r}{c}",
                "space_id": "data_hall_1",
                "attributes": {"rack_count": 1, "rated_power_kW": 5.0},
                "bbox_2d": bbox(x0, y0, x0 + 0.6, y0 + 1.0), "confidence": 0.9,
            })

    # CRAC in the data hall (cooling) -> "CRAC must exist" passes.
    nodes.append({
        "id": "crac_1", "type": "crac", "tag": "CRAC-1", "space_id": "data_hall_1",
        "attributes": {"rated_power_kW": 50.0,
                       "capacity": {"value": 80, "unit": "kW"}},
        "bbox_2d": bbox(16, 4.0, 18, 6.0), "confidence": 0.88,
    })
    # PDU in the electrical room (NO ups on purpose -> violation).
    nodes.append({
        "id": "pdu_1", "type": "pdu", "tag": "PDU-1", "space_id": "elec_1",
        "attributes": {"rated_power_kW": 150.0},
        "bbox_2d": bbox(23, 4.0, 24, 6.0), "confidence": 0.9,
    })

    # Electrical cable from the electrical-room PDU to a data-hall rack:
    # this is what makes data_hall <-> electrical_room "connected".
    edges = [{
        "id": "cable_1", "type": "electrical_cable",
        "from": "pdu_1", "to": rack_ids[0],
        "attributes": {"ampacity_A": 200, "phase": 3}, "confidence": 0.9,
    }]

    graph = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ArchDraft x FlowDraft Unified Graph",
        "meta": {
            "diagram_id": "sample_dc_01", "diagram_type": "FUSED",
            "source_file": "sample.png", "image_width": 1000, "image_height": 800,
            "parse_confidence": 0.9, "parser": "claude",
            "pue": 1.7, "it_power_kW": 200.0, "facility_power_kW": 340.0,
            "target_pue": 1.5,
        },
        "spaces": spaces,
        "nodes": nodes,
        "edges": edges,
    }

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(graph, fh, indent=2)
    print(f"[main] Generated sample graph at {path}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_report(report, objects) -> None:
    """Pretty-print the validation report to the console."""
    line = "=" * 70
    print("\n" + line)
    print("DATA CENTER COMPLIANCE REPORT (TIA-942)")
    print(line)
    print(f"Rules evaluated : {report.checks_run}")
    print(f"Geometry objects: {len(objects)}")
    print(f"Violations found: {len(report.violations)}")
    print(line)

    if report.passed:
        print("\n  [PASS] COMPLIANT — all rules satisfied.\n")
        return

    print("\n  [FAIL] NON-COMPLIANT — the following violations were found:\n")
    for i, v in enumerate(report.violations, start=1):
        loc = v.geometry_id or "—"
        print(f"  {i}. [{v.rule.condition}] "
              f"{v.rule.target_class}/{v.rule.target_type or 'Any'}")
        print(f"     object : {loc}")
        print(f"     detail : {v.message}")
        source = (v.rule.source or "").strip()
        # Suppress null/garbled citations ("null", "2 not specified", lone digits).
        _bad_words = ("null", "not specified", "n/a", "none")
        _is_junk = (
            not source
            or any(w in source.lower() for w in _bad_words)
            or source.rstrip(".").isdigit()          # bare number like "2"
            or (source.split()[0].rstrip(".").isdigit()   # "2 not …", "3. foo"
                and len(source.split()) <= 3)
        )
        if not _is_junk:
            print(f"     source : {source}")
        print()
    print(line + "\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main() -> None:
    load_dotenv(os.path.join(HERE, ".env"))

    # 1. Parse the text/PDF standard(s) into Rules (cached per file).
    reparse = "--reparse" in sys.argv
    print("\n--- Step 1: Parse standard document(s) ---")
    doc_paths = find_standard_documents()
    print(f"[main] Found {len(doc_paths)} standard document(s)"
          f"{' (--reparse: ignoring cache)' if reparse else ''}.")
    rules = []
    for doc_path in doc_paths:
        rules.extend(get_rules_for_document(doc_path, reparse=reparse))
    rules = dedupe_rules(rules)
    print(f"[main] {len(rules)} unique rules after merging all documents.")

    # 2. Persist rules, then reload them from the DB (round-trip proof).
    print("\n--- Step 2: Store & reload rules from database ---")
    session_factory = init_db()
    save_rules(session_factory, rules)
    db_rules = load_rules(session_factory)
    print(f"[main] {len(db_rules)} rules loaded back from the database.")
    for i, r in enumerate(db_rules, start=1):
        val = f"{r.value} {r.unit}".strip() if r.unit else str(r.value)
        print(f"   {i:>3}. [{r.condition}] {r.target_class}/{r.target_type or 'Any'}")
        print(f"        value : {val}")
        if r.description:
            print(f"        desc  : {r.description}")
        if r.source:
            print(f"        source: {r.source}")

    # 3. Locate the floor-plan graph (or generate a sample one).
    print("\n--- Step 3: Locate floor-plan graph ---")
    graph_path = find_graph_document()
    if graph_path is None:
        print("[main] No graph JSON found in data/; generating a sample.")
        generate_sample_graph(SAMPLE_GRAPH)
        graph_path = SAMPLE_GRAPH
    else:
        print(f"[main] Using graph: {graph_path}")

    # 4. Parse the graph geometry.
    print("\n--- Step 4: Parse graph geometry ---")
    objects = parse_graph_json(graph_path)

    # 5. Validate.
    print("\n--- Step 5: Validate geometry against rules ---")
    report = validate(db_rules, objects)

    # 6. Report.
    print_report(report, objects)


if __name__ == "__main__":
    main()
