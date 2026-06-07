"""
MEP P&ID / SLD CV-hybrid parser.

Deterministic local YOLO (models/yolov8n_pid.pt, ~203 classes) owns ALL symbol
coordinates. Claude reads the numbered marks + a text list of those detections
and returns ONLY semantics: per-index tag/attributes (OCR of nameplates) and the
edges (topology) connecting symbols by index. Claude never emits coordinates.

Falls back to the YOLO-only graph builder (04_build_graph) when Claude is
unavailable, so MEP diagrams still parse offline.
"""

import argparse
import base64
import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STRICT_SCHEMA = ROOT / "schemas" / "graph.schema.json"
PROMPT_PID_MARKS = ROOT / "prompts" / "system_pid_marks.md"
CLASS_MAP_PATH = ROOT / "config" / "class_map.yaml"
if not CLASS_MAP_PATH.exists():
    CLASS_MAP_PATH = ROOT / "class_map.yaml"

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_EDGE = 1568
DEFAULT_CONF = 0.15

NODE_TYPES = {
    "instrument", "equipment", "fitting", "valve", "signal", "tower", "unknown",
    "chiller", "pump", "cooling_tower", "ahu", "fcu", "boiler", "fan",
    "transformer", "switchgear", "breaker", "distribution_panel", "meter",
    "sensor", "crac", "crah", "ups", "pdu", "busway", "rack",
}
EDGE_TYPES = {
    "chw_supply", "chw_return", "condenser_water", "air_duct",
    "electrical_cable", "control_signal", "unknown",
}


# ---------------------------------------------------------------------------
# Detection (deterministic)
# ---------------------------------------------------------------------------
def detect_symbols_yolo(path, conf=DEFAULT_CONF, weights_path=None):
    """Run the local YOLO detector; return region dicts numbered for set-of-marks."""

    from src import preprocess as prep_mod
    from src import detect_yolo as det_mod

    prep = prep_mod.preprocess(path)
    dets = det_mod.detect(prep, weights_path=weights_path, conf=conf)

    img_w, img_h = prep["width"], prep["height"]
    regions = []
    for d in dets:
        bbox_2d = d["bbox_2d"]
        bbox_px = d.get("bbox_px")
        if bbox_px is None:
            cx, cy, bw, bh = bbox_2d
            bbox_px = [
                (cx - bw / 2) * img_w, (cy - bh / 2) * img_h,
                (cx + bw / 2) * img_w, (cy + bh / 2) * img_h,
            ]
        regions.append(
            {
                "index": 0,
                "sub_type": d.get("sub_type") or "unknown",
                "class_hint": d.get("sub_type") or "unknown",
                "group": "symbol",
                "bbox_2d": [round(float(v), 5) for v in bbox_2d],
                "bbox_px": [float(v) for v in bbox_px],
                "confidence": float(d.get("confidence") or 0.5),
            }
        )

    # top-left reading order, then number
    regions.sort(key=lambda r: (round(r["bbox_2d"][1], 2), r["bbox_2d"][0]))
    for i, reg in enumerate(regions):
        reg["index"] = i + 1
    return regions, prep["image"], img_w, img_h, prep["source_file"]


# ---------------------------------------------------------------------------
# Claude semantics (topology + OCR; NO coordinates)
# ---------------------------------------------------------------------------
def _semantics_schema():
    node_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "tag"],
        "properties": {
            "index": {"type": "integer"},
            "tag": {"type": "string"},
            "type_refine": {"type": "string"},
            "rated_power_kW": {"type": "number"},
            "voltage_V": {"type": "number"},
            "ampacity_A": {"type": "number"},
        },
    }
    edge_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["from_index", "to_index", "type"],
        "properties": {
            "from_index": {"type": "integer"},
            "to_index": {"type": "integer"},
            "type": {"type": "string"},
            "medium": {"type": "string"},
        },
    }
    meta_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["diagram_id", "diagram_type", "source_file", "parse_confidence"],
        "properties": {
            "diagram_id": {"type": "string"},
            "diagram_type": {"type": "string"},
            "source_file": {"type": "string"},
            "parse_confidence": {"type": "number"},
            "title": {"type": "string"},
            "drawing_number": {"type": "string"},
        },
    }

    from src import cv_marks as marks
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["meta", "nodes", "edges"],
        "properties": {
            "meta": meta_item,
            "nodes": {"type": "array", "items": node_item},
            "edges": {"type": "array", "items": edge_item},
            "handwriting": {"type": "array", "items": marks.handwriting_schema_item()},
        },
    }


def _encode_image_bgr(img, max_edge=MAX_EDGE):
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side > max_edge:
        scale = max_edge / long_side
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("Failed to encode image")
    return base64.standard_b64encode(buf.tobytes()).decode("utf-8"), "image/png"


def _call_claude(client, model, system, schema, b64, media_type, extra_text=""):
    return client.messages.create(
        model=model,
        max_tokens=16384,
        system=system,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    },
                    {"type": "text", "text": extra_text or "Read tags and build edges now."},
                ],
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )


def _extract_json(response):
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise RuntimeError("Claude response truncated (max_tokens).")
    parts = [getattr(b, "text", "") for b in response.content if getattr(b, "text", None)]
    return json.loads("".join(parts).strip())


def claude_semantics(image_marked, regions, source_file, diagram_type, model=DEFAULT_MODEL, handwriting=False):
    """Claude returns per-index tags/attributes + edges by index. One repair retry."""
    from src.secrets_util import get_anthropic_key

    key = get_anthropic_key()
    if not key:
        raise FileNotFoundError(
            "No Anthropic API key (set ANTHROPIC_API_KEY or models/secretapi.txt)"
        )
    import anthropic

    from src import cv_marks as marks
    client = anthropic.Anthropic(api_key=key)
    system = PROMPT_PID_MARKS.read_text(encoding="utf-8")
    schema = _semantics_schema()
    b64, media_type = _encode_image_bgr(image_marked)
    detections = marks.stringify_detections(symbols=regions)
    extra = (
        f"diagram_type = {diagram_type}. Read the tag/nameplate text for each numbered symbol "
        "and connect the symbols that are joined by pipes/wires/ducts. Use ONLY these box "
        "indices; do NOT output any coordinates.\n\n"
        f"<detections>\n{detections}\n</detections>"
    )
    hw = marks.handwriting_extra_text(handwriting)
    if hw:
        extra = f"{extra}\n\n{hw}"

    try:
        response = _call_claude(client, model, system, schema, b64, media_type, extra_text=extra)
        data = _extract_json(response)
    except Exception as first_err:
        repair = f"{extra}\n\nPrevious attempt failed: {first_err}. Return valid JSON only."
        response = _call_claude(client, model, system, schema, b64, media_type, extra_text=repair)
        data = _extract_json(response)

    data.setdefault("meta", {})
    data["meta"]["source_file"] = source_file
    return data


# ---------------------------------------------------------------------------
# Build graph (geometry from YOLO, semantics from Claude)
# ---------------------------------------------------------------------------
def _load_class_map():
    import yaml

    try:
        with open(CLASS_MAP_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def build_graph(regions, semantics, meta, diagram_type, handwriting=False):
    """Merge YOLO geometry with Claude tags/edges; validate vs strict schema."""

    from src import build_graph as build_mod
    from src import claude_parse as claude_mod
    class_map = _load_class_map()

    node_sem = {n["index"]: n for n in semantics.get("nodes") or []}
    index_to_id = {reg["index"]: f"node-{reg['index']}" for reg in regions}

    nodes = []
    for reg in regions:
        sem = node_sem.get(reg["index"], {})
        sub_type = reg["sub_type"]
        eng_type = build_mod.map_sub_type_to_type(sub_type, class_map)
        # Claude may refine the engineering type when the symbol class is ambiguous.
        refine = (sem.get("type_refine") or "").strip().lower()
        if refine in NODE_TYPES:
            eng_type = refine

        attrs = claude_mod._drop_empty_values(
            {
                "rated_power_kW": sem.get("rated_power_kW"),
                "voltage_V": sem.get("voltage_V"),
                "ampacity_A": sem.get("ampacity_A"),
            }
        )
        node = {
            "id": index_to_id[reg["index"]],
            "type": eng_type,
            "sub_type": sub_type,
            "tag": sem.get("tag") or f"N{reg['index']:02d}",
            "attributes": attrs,
            "bbox_2d": [round(float(v), 5) for v in reg["bbox_2d"]],
            "confidence": round(float(reg["confidence"]), 3),
        }
        nodes.append(node)

    edges = []
    seen = set()
    for i, e in enumerate(semantics.get("edges") or []):
        a = index_to_id.get(e.get("from_index"))
        b = index_to_id.get(e.get("to_index"))
        if not a or not b or a == b:
            continue
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        etype = (e.get("type") or "unknown").strip()
        if etype not in EDGE_TYPES:
            etype = "unknown"
        attrs = {}
        if e.get("medium"):
            attrs["medium"] = str(e["medium"])
        edges.append(
            {
                "id": f"edge-{i + 1}",
                "type": etype,
                "from": a,
                "to": b,
                "attributes": attrs,
                "confidence": 0.7,
            }
        )

    graph = {
        "meta": dict(meta),
        "spaces": [],
        "walls": [],
        "fixtures": [],
        "nodes": nodes,
        "edges": edges,
    }
    from src import cv_marks as marks
    marks.attach_handwriting_annotations(graph, semantics, enabled=handwriting)
    return graph


def validate(graph):
    import jsonschema

    with open(STRICT_SCHEMA, "r", encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(graph, schema)
    return True


def _fallback_yolo(path, diagram_type, conf, reason):
    """YOLO-only graph builder (04) when Claude is unavailable."""

    from src import build_graph as build_mod
    graph = build_mod.parse_diagram(path, diagram_type=diagram_type, conf=conf)
    graph.setdefault("meta", {})
    graph["meta"]["parser"] = "yolo"
    graph["meta"]["warnings"] = list(graph["meta"].get("warnings") or []) + [
        f"pid_hybrid_fallback: {reason}; used YOLO-only builder"
    ]
    return graph


def parse_pid_hybrid(path, diagram_type="PID", conf=DEFAULT_CONF, model=DEFAULT_MODEL,
                     weights_path=None, debug_marks_path=None, handwriting=False):
    """
    Full MEP CV-hybrid pipeline. Returns a graph validated against graph.schema.json.
    YOLO supplies symbol geometry; Claude supplies tags + topology (set-of-marks).
    """

    if diagram_type not in ("PID", "SLD"):
        diagram_type = "PID"

    # 1. Deterministic detection (raises if weights missing/COCO -> fall back).
    try:
        regions, image, img_w, img_h, source_file = detect_symbols_yolo(
            path, conf=conf, weights_path=weights_path
        )
    except Exception as exc:
        return _fallback_yolo(path, diagram_type, conf, f"detector error {type(exc).__name__}: {exc}")

    if not regions:
        return _fallback_yolo(path, diagram_type, conf, "no symbols detected")

    meta = {
        "diagram_id": f"pid-hybrid-{Path(path).stem}",
        "diagram_type": diagram_type,
        "source_file": source_file,
        "image_width": img_w,
        "image_height": img_h,
        "parse_confidence": 0.0,
        "parser": "yolo+claude",
        "topology": "set_of_marks",
        "warnings": [],
    }

    from src import cv_marks as marks
    marked = marks.draw_numbered_marks(image, regions)
    if debug_marks_path:
        cv2.imwrite(str(debug_marks_path), marked)

    # 2. Claude semantics (topology + OCR). Fall back to YOLO-only on failure.
    try:
        semantics = claude_semantics(
            marked, regions, source_file, diagram_type, model=model, handwriting=handwriting
        )
    except Exception as exc:
        return _fallback_yolo(path, diagram_type, conf, f"claude error {type(exc).__name__}: {exc}")

    meta["parse_confidence"] = float(semantics.get("meta", {}).get("parse_confidence") or 0.8)
    for key_name in ("title", "drawing_number"):
        val = semantics.get("meta", {}).get(key_name)
        if val:
            meta[key_name] = val

    graph = build_graph(regions, semantics, meta, diagram_type, handwriting=handwriting)

    try:
        validate(graph)
        return graph
    except Exception as first_err:
        graph["meta"].setdefault("warnings", []).append(f"validation_repair: {first_err}")
        from src.secrets_util import get_anthropic_key
        import anthropic

        client = anthropic.Anthropic(api_key=get_anthropic_key())
        system = PROMPT_PID_MARKS.read_text(encoding="utf-8")
        schema = _semantics_schema()
        b64, media_type = _encode_image_bgr(marked)
        repair = f"Previous output failed validation: {first_err}. Fix and return valid JSON only."
        response = _call_claude(client, model, system, schema, b64, media_type, extra_text=repair)
        semantics = _extract_json(response)
        semantics.setdefault("meta", {})["source_file"] = source_file
        graph = build_graph(regions, semantics, meta, diagram_type, handwriting=handwriting)
        validate(graph)
        return graph


def main():
    parser = argparse.ArgumentParser(description="Parse a P&ID/SLD (YOLO + Claude CV-hybrid)")
    parser.add_argument("image")
    parser.add_argument("--type", default="PID", choices=["PID", "SLD"])
    parser.add_argument("--conf", type=float, default=DEFAULT_CONF)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--weights", default=None)
    parser.add_argument("--debug-marks", default=None, help="Save numbered marks image")
    parser.add_argument("--out", default=None)
    parser.add_argument("--handwriting", action="store_true", help="OCR handwritten/red-ink notes into annotations[]")
    args = parser.parse_args()

    graph = parse_pid_hybrid(
        args.image, diagram_type=args.type, conf=args.conf, model=args.model,
        weights_path=args.weights, debug_marks_path=args.debug_marks,
        handwriting=args.handwriting,
    )
    out = Path(args.out) if args.out else ROOT / "data" / f"parsed_pid_{Path(args.image).stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    print(
        "Wrote", out,
        "| parser:", graph["meta"].get("parser"),
        "| nodes:", len(graph["nodes"]),
        "edges:", len(graph["edges"]),
    )


if __name__ == "__main__":
    main()
