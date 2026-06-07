"""
Floor-plan CV-hybrid parser: Roboflow detects pixel-accurate room boxes;
Claude labels numbered marks (Set-of-Marks). No coordinates from Claude.

Detector: Roboflow hosted `architectural-blueprint/2` (object detection).
Recognition + OCR: Claude (verbatim area/dimensions, schema category, habitable).

Falls back to the Claude-only coordinate parser (07_claude_parse) when Roboflow
is unreachable or finds too few rooms.
"""

import argparse
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STRICT_SCHEMA = ROOT / "schemas" / "graph.schema.json"
PROMPT_MARKS = ROOT / "prompts" / "system_floorplan_marks.md"

ROBOFLOW_URL = "https://serverless.roboflow.com"
ROBOFLOW_DETECT_URL = "https://detect.roboflow.com"
ROBOFLOW_MODEL_ID = "architectural-blueprint/2"
# Openings/fixtures detector (doors, windows). architectural-blueprint only
# outputs rooms, so a second model captures the small elements needed for 3D
# reconstruction. Verified working default: floor-plans-500-r7jy4/1 emits
# door/window/zone. Swappable via env ROBOFLOW_FIXTURE_MODEL (e.g. blue-print/2,
# or any model with sanitary classes like toilet/sink).
FIXTURE_MODEL_ID = os.environ.get("ROBOFLOW_FIXTURE_MODEL", "floor-plans-500-r7jy4/1")
# Cloudflare in front of Roboflow blocks the default python-urllib User-Agent (err 1010).
_HTTP_UA = "Mozilla/5.0 (compatible; flowdraft-cv-hybrid/1.0)"
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_EDGE = 1568
DEFAULT_CONF = 0.25  # detector recall/precision balance for this hosted model
FIXTURE_CONF = float(os.environ.get("ROBOFLOW_FIXTURE_CONF", "0.30"))

# Map fixture-detector classes -> schema fixture categories.
FIXTURE_CLASS_MAP = {
    "door": "door", "doors": "door", "single_door": "door",
    "double_door": "double_door", "sliding_door": "sliding_door",
    "garage_door": "garage_door", "folding_door": "folding_door",
    "window": "window", "windows": "window",
    "opening": "opening",
    "toilet": "toilet", "wc": "toilet", "sink": "sink", "washbasin": "sink",
    "bathtub": "bathtub", "tub": "bathtub", "bath": "bathtub",
    "shower": "shower", "bidet": "bidet",
    "stove": "stove", "range": "stove", "cooktop": "stove",
    "refrigerator": "refrigerator", "fridge": "refrigerator",
    "dishwasher": "dishwasher", "washer": "washer", "washing_machine": "washer",
    "dryer": "dryer", "stairs": "stairs", "staircase": "stairs",
    "elevator": "elevator_car", "column": "column",
}
# Classes the fixture model emits that are actually rooms/zones (ignored here;
# rooms come from the architectural-blueprint model).
_FIXTURE_ZONE_CLASSES = {"zone", "room", "space", "background"}
# Quality gate: the architectural-blueprint model only generalizes to apartment-style
# plans. When detections are sparse or barely cover the drawing (villas, schematics),
# fall back to the Claude coordinate parser instead of emitting a near-empty graph.
MIN_ROOMS = 5
MIN_COVERAGE = 0.10  # union of detected room polygons as a fraction of the image
DEFAULT_SCALE_M_PER_PX = 0.01  # only used to estimate an area when nothing else is known

# Strict graph.schema.json space categories (final enum Claude must pick from).
STRICT_CATEGORIES = {
    "data_hall", "electrical_room", "plant_room", "mechanical_room", "server_room",
    "office", "meeting_room", "reception", "lobby", "corridor", "hallway",
    "stairwell", "elevator", "bedroom", "bathroom", "toilet", "kitchen", "pantry",
    "living_room", "dining_room", "garage", "storage", "closet", "utility",
    "laundry", "balcony", "terrace", "patio", "garden", "retail", "classroom",
    "unknown",
}

# Strict graph.schema.json fixture categories (final enum for fixtures[]).
FIXTURE_CATEGORIES = {
    "door", "sliding_door", "double_door", "folding_door", "garage_door",
    "window", "opening", "toilet", "sink", "bathtub", "shower", "bidet",
    "stove", "refrigerator", "dishwasher", "washer", "dryer",
    "stairs", "elevator_car", "column", "unknown",
}

# Roboflow class -> schema category hint (fallback only; Claude refines).
ROBO_CLASS_MAP = {
    "space_bedroom": "bedroom",
    "space_kitchen": "kitchen",
    "space_living_room": "living_room",
    "space_toilet": "toilet",
    "space_balconi": "balcony",
    "space_staircase": "stairwell",
    "space_elevator": "elevator",
    "space_elevator_hall": "lobby",
    "space_dressroom": "closet",
    "space_front": "corridor",
    "space_outdoor_room": "terrace",
    "space_multipurpose_space": "unknown",
    "space_other": "unknown",
}


def _clamp01(v):
    return max(0.0, min(1.0, float(v)))


def _pick_category(claude_cat, class_hint):
    """Final category: trust Claude if it picked a valid enum, else map the Roboflow class."""
    if claude_cat:
        c = str(claude_cat).strip().lower().replace(" ", "_").replace("-", "_")
        if c in STRICT_CATEGORIES:
            return c
    return ROBO_CLASS_MAP.get(class_hint, "unknown")


def _pick_fixture_category(claude_cat, class_hint):
    """Final fixture category: trust a valid Claude pick, else map the detector class."""
    if claude_cat:
        c = str(claude_cat).strip().lower().replace(" ", "_").replace("-", "_")
        if c in FIXTURE_CATEGORIES:
            return c
    return FIXTURE_CLASS_MAP.get(str(class_hint).lower(), "unknown")


def load_image(path):
    """Load PNG/JPG/WEBP; PIL fallback when cv2 cannot read webp."""
    path = Path(path)
    img = cv2.imread(str(path))
    if img is not None:
        return img, path.name
    try:
        from PIL import Image

        pil = Image.open(path).convert("RGB")
        arr = np.array(pil)
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR), path.name
    except Exception as exc:
        raise FileNotFoundError(f"Cannot read image: {path}") from exc


def _iou(a, b):
    """IoU of two bbox_px [x1,y1,x2,y2]."""
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter <= 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter + 1e-9)


def _dedupe_regions(regions, iou_thresh=0.55):
    """Keep higher-confidence (then larger) box when two regions overlap heavily."""
    if not regions:
        return regions
    order = sorted(regions, key=lambda r: (r.get("confidence", 0), r["area_px"]), reverse=True)
    kept = []
    for reg in order:
        if any(_iou(reg["bbox_px"], k["bbox_px"]) > iou_thresh for k in kept):
            continue
        kept.append(reg)
    return kept


def _roboflow_infer(png_bytes, key, conf, model_id=ROBOFLOW_MODEL_ID):
    """POST base64 PNG to the Roboflow hosted detect API; return parsed JSON.

    Prefers inference_sdk when available; otherwise a dependency-free urllib POST.
    """
    # Try the official lightweight client first.
    try:
        from inference_sdk import InferenceHTTPClient, InferenceConfiguration

        client = InferenceHTTPClient(api_url=ROBOFLOW_URL, api_key=key)
        client.configure(InferenceConfiguration(confidence_threshold=conf))
        tmp = ROOT / "data" / "_roboflow_tmp.png"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(png_bytes)
        try:
            return client.infer(str(tmp), model_id=model_id)
        finally:
            tmp.unlink(missing_ok=True)
    except ImportError:
        pass

    # Dependency-free fallback: raw HTTP POST to the classic hosted detect API.
    # A browser-like User-Agent is required (Cloudflare blocks python-urllib).
    b64 = base64.b64encode(png_bytes)
    url = (
        f"{ROBOFLOW_DETECT_URL}/{model_id}"
        f"?api_key={key}&confidence={int(conf * 100)}&overlap=30&format=json"
    )
    req = urllib.request.Request(
        url,
        data=b64,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _HTTP_UA,
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def detect_rooms_roboflow(image, key, conf=0.40):
    """Run the Roboflow detector; return pixel-accurate region dicts for Set-of-Marks."""
    h, w = image.shape[:2]
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("Failed to encode image for Roboflow")
    result = _roboflow_infer(buf.tobytes(), key, conf)

    preds = result.get("predictions") or []
    img_meta = result.get("image") or {}
    rw = float(img_meta.get("width") or w)
    rh = float(img_meta.get("height") or h)

    regions = []
    for p in preds:
        cls = str(p.get("class") or "").lower()
        if cls == "background":
            continue
        cx, cy = float(p["x"]), float(p["y"])
        bw, bh = float(p["width"]), float(p["height"])
        x1, y1 = cx - bw / 2, cy - bh / 2
        x2, y2 = cx + bw / 2, cy + bh / 2
        # normalize against the resolution Roboflow reported, scaled to our pixels
        nx1, ny1 = _clamp01(x1 / rw), _clamp01(y1 / rh)
        nx2, ny2 = _clamp01(x2 / rw), _clamp01(y2 / rh)
        if nx2 - nx1 < 0.01 or ny2 - ny1 < 0.01:
            continue
        poly = [[nx1, ny1], [nx2, ny1], [nx2, ny2], [nx1, ny2]]
        regions.append(
            {
                "index": 0,
                "class_hint": cls,
                "bbox_px": [x1 / rw * w, y1 / rh * h, x2 / rw * w, y2 / rh * h],
                "bbox_2d": [
                    round((nx1 + nx2) / 2, 5),
                    round((ny1 + ny2) / 2, 5),
                    round(nx2 - nx1, 5),
                    round(ny2 - ny1, 5),
                ],
                "polygon_2d": [[round(px, 5), round(py, 5)] for px, py in poly],
                "area_px": (x2 - x1) * (y2 - y1),
                "confidence": float(p.get("confidence") or 0.5),
            }
        )

    regions = _dedupe_regions(regions, iou_thresh=0.55)
    regions.sort(key=lambda r: (r["bbox_2d"][1], r["bbox_2d"][0]))
    for i, reg in enumerate(regions):
        reg["index"] = i + 1
        reg["group"] = "space"
    return regions


def detect_fixtures_roboflow(image, key, conf=FIXTURE_CONF, model_id=FIXTURE_MODEL_ID):
    """
    Run the openings/fixtures detector; return pixel-accurate region dicts.
    Rooms/zones emitted by this model are dropped (rooms come from the
    architectural-blueprint model). Returns [] on any error so the floor-plan
    parse degrades to rooms-only instead of failing.
    """
    h, w = image.shape[:2]
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        return []
    try:
        result = _roboflow_infer(buf.tobytes(), key, conf, model_id=model_id)
    except Exception:
        return []

    preds = result.get("predictions") or []
    img_meta = result.get("image") or {}
    rw = float(img_meta.get("width") or w)
    rh = float(img_meta.get("height") or h)

    regions = []
    for p in preds:
        cls = str(p.get("class") or "").lower()
        if cls in _FIXTURE_ZONE_CLASSES:
            continue
        cx, cy = float(p["x"]), float(p["y"])
        bw, bh = float(p["width"]), float(p["height"])
        x1, y1 = cx - bw / 2, cy - bh / 2
        x2, y2 = cx + bw / 2, cy + bh / 2
        nx1, ny1 = _clamp01(x1 / rw), _clamp01(y1 / rh)
        nx2, ny2 = _clamp01(x2 / rw), _clamp01(y2 / rh)
        if nx2 - nx1 < 0.002 or ny2 - ny1 < 0.002:
            continue
        is_opening = FIXTURE_CLASS_MAP.get(cls, "") in ("door", "window", "opening",
                                                        "sliding_door", "double_door",
                                                        "folding_door", "garage_door")
        regions.append(
            {
                "index": 0,
                "class_hint": cls,
                "group": "opening" if is_opening else "fixture",
                "bbox_px": [x1 / rw * w, y1 / rh * h, x2 / rw * w, y2 / rh * h],
                "bbox_2d": [
                    round((nx1 + nx2) / 2, 5),
                    round((ny1 + ny2) / 2, 5),
                    round(nx2 - nx1, 5),
                    round(ny2 - ny1, 5),
                ],
                "polygon_2d": [
                    [round(nx1, 5), round(ny1, 5)], [round(nx2, 5), round(ny1, 5)],
                    [round(nx2, 5), round(ny2, 5)], [round(nx1, 5), round(ny2, 5)],
                ],
                "area_px": (x2 - x1) * (y2 - y1),
                "confidence": float(p.get("confidence") or 0.5),
            }
        )

    regions = _dedupe_regions(regions, iou_thresh=0.55)
    regions.sort(key=lambda r: (r["bbox_2d"][1], r["bbox_2d"][0]))
    return regions


def _coverage(regions):
    """Fraction of the (normalized) image covered by the union of detected room boxes."""
    if not regions:
        return 0.0
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union

        polys = [Polygon(r["polygon_2d"]) for r in regions if len(r.get("polygon_2d") or []) >= 3]
        polys = [p for p in polys if p.is_valid and p.area > 0]
        if not polys:
            return 0.0
        return float(unary_union(polys).area)
    except Exception:
        # Deduped boxes barely overlap, so summed areas approximate the union.
        return float(sum(r["bbox_2d"][2] * r["bbox_2d"][3] for r in regions))


def draw_marks(image, regions):
    """Draw numbered red boxes; return annotated copy (for Set-of-Marks labeling)."""
    out = image.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    for reg in regions:
        x1, y1, x2, y2 = [int(v) for v in reg["bbox_px"]]
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255), 2)
        label = str(reg["index"])
        scale = max(0.5, min(1.4, (x2 - x1) / 90))
        (tw, th), _ = cv2.getTextSize(label, font, scale, 2)
        tx = x1 + 4
        ty = y1 + th + 6
        cv2.rectangle(out, (tx - 2, ty - th - 4), (tx + tw + 4, ty + 4), (255, 255, 255), -1)
        cv2.putText(out, label, (tx, ty), font, scale, (0, 0, 200), 2, cv2.LINE_AA)
    return out


def _labels_schema():
    """Claude structured-output schema for set-of-marks room + fixture labeling."""
    label_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "name", "category", "confidence"],
        "properties": {
            "index": {"type": "integer"},
            "name": {"type": "string"},
            "category": {"type": "string"},
            "area_raw": {"type": "string"},
            "dimensions_raw": {"type": "string"},
            "room_number": {"type": "string"},
            "habitable": {"type": "boolean"},
            "confidence": {"type": "number"},
        },
    }
    fixture_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "category", "confidence"],
        "properties": {
            "index": {"type": "integer"},
            "category": {"type": "string"},
            "name": {"type": "string"},
            "room_index": {"type": "integer"},
            "confidence": {"type": "number"},
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
            "level": {"type": "string"},
            "scale_ratio": {"type": "string"},
        },
    }

    from src import cv_marks as marks
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["meta", "labels"],
        "properties": {
            "meta": meta_item,
            "labels": {"type": "array", "items": label_item},
            "fixtures": {"type": "array", "items": fixture_item},
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
        max_tokens=8192,
        system=system,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    },
                    {"type": "text", "text": extra_text or "Label every numbered room box."},
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


def label_rooms(image_marked, regions, source_file, model=DEFAULT_MODEL, fixture_regions=None, handwriting=False):
    """Claude labels each numbered room (and fixture); one repair retry on JSON failure."""
    from src.secrets_util import get_anthropic_key

    key = get_anthropic_key()
    if not key:
        raise FileNotFoundError(
            "No Anthropic API key (set ANTHROPIC_API_KEY or models/secretapi.txt)"
        )
    import anthropic

    from src import cv_marks as marks
    client = anthropic.Anthropic(api_key=key)
    system = PROMPT_MARKS.read_text(encoding="utf-8")
    schema = _labels_schema()
    b64, media_type = _encode_image_bgr(image_marked)

    room_idx = ", ".join(str(r["index"]) for r in regions)
    fixture_regions = fixture_regions or []
    openings = [r for r in fixture_regions if r.get("group") == "opening"]
    fittings = [r for r in fixture_regions if r.get("group") != "opening"]
    detections = marks.stringify_detections(
        spaces=regions, openings=openings, fixtures=fittings
    )
    extra = (
        f"Label rooms numbered: {room_idx}. Return exactly one `labels` entry per ROOM box index. "
        "For every numbered door/window/fixture box, return one `fixtures` entry with its index, "
        "a category, and the room_index it belongs to. Use the exact box indices below; "
        "do NOT output any coordinates.\n\n"
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


def build_graph(regions, label_data, meta, fixture_regions=None, handwriting=False):
    """Merge Roboflow geometry with Claude labels; reuse 07 enrichment/cleanup."""

    label_map = {lab["index"]: lab for lab in label_data.get("labels") or []}
    spaces = []
    for reg in regions:
        lab = label_map.get(reg["index"], {})
        name = lab.get("name") or f"Room {reg['index']}"
        category = _pick_category(lab.get("category"), reg["class_hint"])
        poly = [[_clamp01(x), _clamp01(y)] for x, y in reg["polygon_2d"]]
        sp = {
            "id": f"space-{reg['index']}",
            "name": name,
            "category": category,
            "polygon_2d": poly,
            "bbox_2d": [_clamp01(v) for v in reg["bbox_2d"]],
            "confidence": float(lab.get("confidence") or reg.get("confidence") or 0.75),
        }
        if lab.get("area_raw"):
            sp["area_raw"] = lab["area_raw"]
        if lab.get("dimensions_raw"):
            sp["dimensions_raw"] = lab["dimensions_raw"]
        if lab.get("room_number"):
            sp["room_number"] = lab["room_number"]
        if lab.get("habitable") is not None:
            sp["habitable"] = bool(lab["habitable"])
        spaces.append(sp)

    # Fixtures: geometry from the detector, category from Claude (set-of-marks).
    fixture_label_map = {f["index"]: f for f in label_data.get("fixtures") or []}
    room_index_to_id = {reg["index"]: f"space-{reg['index']}" for reg in regions}
    fixtures = []
    for reg in fixture_regions or []:
        lab = fixture_label_map.get(reg["index"], {})
        category = _pick_fixture_category(lab.get("category"), reg["class_hint"])
        fx = {
            "id": f"fixture-{reg['index']}",
            "category": category,
            "bbox_2d": [_clamp01(v) for v in reg["bbox_2d"]],
            "polygon_2d": [[_clamp01(x), _clamp01(y)] for x, y in reg["polygon_2d"]],
            "source": "roboflow",
            "confidence": float(lab.get("confidence") or reg.get("confidence") or 0.7),
        }
        if lab.get("name"):
            fx["name"] = lab["name"]
        room_idx = lab.get("room_index")
        if room_idx in room_index_to_id:
            fx["space_id"] = room_index_to_id[room_idx]
        fixtures.append(fx)

    graph = {
        "meta": dict(meta),
        "spaces": spaces,
        "walls": [],
        "fixtures": fixtures,
        "nodes": [],
        "edges": [],
    }

    # Reuse the floor-plan enrichment + geometry cleanup from the Claude parser.
    from src import claude_parse as claude_mod
    graph = claude_mod._enrich_spaces(graph)
    graph = claude_mod._snap_vertices(graph)
    graph = claude_mod._resolve_overlaps(graph)
    graph = claude_mod._enrich_spaces(graph)

    # Guarantee a positive area_m2 for schema compliance (rooms with no annotation
    # and no derived scale fall back to an assumed scale).
    img_w = graph["meta"].get("image_width") or 1
    img_h = graph["meta"].get("image_height") or 1
    for sp in graph["spaces"]:
        if sp.get("area_m2") is None:
            area_norm = claude_mod._shoelace_norm(sp.get("polygon_2d") or [])
            est = area_norm * img_w * img_h * (DEFAULT_SCALE_M_PER_PX ** 2)
            sp["area_m2"] = round(max(est, 0.1), 2)
            sp["area_source"] = "estimated"

    # Geometry-based space_id fallback (point-in-polygon) + deterministic sizing.
    _assign_fixture_spaces(graph)
    scale = graph["meta"].get("scale_m_per_px")
    if scale:
        from src import cv_marks as marks
        marks.derive_sizes(graph["fixtures"], scale, img_w, img_h)
    elif graph["fixtures"]:
        graph["meta"].setdefault("warnings", []).append(
            "uncalibrated: no scale_m_per_px found; fixture width_m/height_m omitted "
            "(provide a known dimension to enable 3D sizing)"
        )
        graph["meta"].setdefault("scale_source", "none")
    from src import cv_marks as marks
    marks.attach_handwriting_annotations(graph, label_data, enabled=handwriting)
    return graph


def _assign_fixture_spaces(graph):
    """Point-in-polygon: assign each fixture to the room whose polygon contains it."""
    fixtures = graph.get("fixtures") or []
    spaces = graph.get("spaces") or []
    if not fixtures or not spaces:
        return graph
    try:
        from shapely.geometry import Point, Polygon
    except ImportError:
        return graph

    polys = []
    for sp in spaces:
        pts = sp.get("polygon_2d") or []
        if len(pts) >= 3:
            try:
                polys.append((sp["id"], Polygon(pts)))
            except Exception:
                continue
    for fx in fixtures:
        if fx.get("space_id"):
            continue
        bbox = fx.get("bbox_2d")
        if not bbox:
            continue
        pt = Point(bbox[0], bbox[1])
        best = None
        best_dist = 1e9
        for sid, poly in polys:
            if poly.contains(pt):
                best = sid
                break
            d = poly.distance(pt)
            if d < best_dist:
                best_dist, best = d, sid
        # openings sit on walls between rooms -> nearest room is acceptable
        if best and (best_dist < 0.03 or fx.get("space_id") is None):
            fx["space_id"] = best
    return graph


def validate(graph):
    import jsonschema

    with open(STRICT_SCHEMA, "r", encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(graph, schema)
    return True


def _fixtures_from_regions(fixture_regions):
    """Build fixtures[] straight from detector classes (no Claude labels)."""
    fixtures = []
    for reg in fixture_regions or []:
        fixtures.append(
            {
                "id": f"fixture-{reg['index']}",
                "category": _pick_fixture_category(None, reg["class_hint"]),
                "bbox_2d": [_clamp01(v) for v in reg["bbox_2d"]],
                "polygon_2d": [[_clamp01(x), _clamp01(y)] for x, y in reg["polygon_2d"]],
                "source": "roboflow",
                "confidence": float(reg.get("confidence") or 0.7),
            }
        )
    return fixtures


def _fallback_claude(path, model, reason, fixture_regions=None, handwriting=False):
    """
    Claude-only coordinate parser when the ROOM detector is unusable. Detected
    fixtures (a separate model) are still attached so doors/windows/sanitary are
    captured even when room detection is weak.
    """

    from src import claude_parse as claude_mod
    graph = claude_mod.parse_with_claude(
        path, diagram_type="FLOORPLAN", model=model, handwriting=handwriting
    )
    graph.setdefault("meta", {})
    graph["meta"]["warnings"] = list(graph["meta"].get("warnings") or []) + [
        f"cv_hybrid_fallback: {reason}; used Claude coordinate parser"
    ]

    fixtures = _fixtures_from_regions(fixture_regions)
    if fixtures:
        graph["fixtures"] = fixtures
        graph["meta"]["warnings"].append(
            f"fixtures_from_detector: attached {len(fixtures)} doors/windows/fixtures "
            f"(model={FIXTURE_MODEL_ID}) despite weak room detection"
        )
        _assign_fixture_spaces(graph)
        scale = graph["meta"].get("scale_m_per_px")
        img_w = graph["meta"].get("image_width") or 1
        img_h = graph["meta"].get("image_height") or 1
        if scale:
            from src import cv_marks as marks
            marks.derive_sizes(graph["fixtures"], scale, img_w, img_h)
    return graph


def parse_floorplan_hybrid(path, conf=DEFAULT_CONF, model=DEFAULT_MODEL, debug_marks_path=None, handwriting=False):
    """
    Full CV-hybrid pipeline. Returns a graph dict validated against graph.schema.json.
    Roboflow supplies room geometry; Claude supplies recognition (set-of-marks).
    """
    from src.secrets_util import get_roboflow_key

    image, source_file = load_image(path)
    h, w = image.shape[:2]

    key = get_roboflow_key()
    if not key:
        return _fallback_claude(path, model, "no Roboflow API key", handwriting=handwriting)

    try:
        regions = detect_rooms_roboflow(image, key, conf=conf)
    except Exception as exc:
        return _fallback_claude(
            path, model, f"roboflow error {type(exc).__name__}: {exc}", handwriting=handwriting
        )

    # Openings/fixtures detector runs independently of the room detector, so
    # doors/windows/sanitary are captured even when room detection is weak.
    # Numbered AFTER rooms so the single marks image has unique indices.
    fixture_regions = detect_fixtures_roboflow(image, key)
    for i, reg in enumerate(fixture_regions):
        reg["index"] = len(regions) + i + 1

    # Quality gate: only trust the ROOM detector when it found enough rooms and
    # they cover a meaningful share of the drawing (else Claude-only for rooms,
    # but detected fixtures are still attached).
    cov = _coverage(regions)
    if len(regions) < MIN_ROOMS or cov < MIN_COVERAGE:
        return _fallback_claude(
            path,
            model,
            f"weak detection (rooms={len(regions)}, coverage={cov:.2f}); "
            f"detector not suited to this plan style",
            fixture_regions=fixture_regions,
            handwriting=handwriting,
        )

    meta = {
        "diagram_id": f"fp-hybrid-{Path(path).stem}",
        "diagram_type": "FLOORPLAN",
        "source_file": source_file,
        "image_width": w,
        "image_height": h,
        "parse_confidence": 0.0,
        "parser": "roboflow+claude",
        "topology": "set_of_marks",
        "warnings": [],
    }

    if not fixture_regions:
        meta["warnings"].append(
            "no_fixtures_detected: openings/fixtures model returned nothing "
            f"(model={FIXTURE_MODEL_ID})"
        )

    from src import cv_marks as marks
    marked = marks.draw_numbered_marks(image, list(regions) + list(fixture_regions))
    if debug_marks_path:
        cv2.imwrite(str(debug_marks_path), marked)

    label_data = label_rooms(
        marked, regions, source_file, model=model,
        fixture_regions=fixture_regions, handwriting=handwriting,
    )
    meta["parse_confidence"] = float(
        label_data.get("meta", {}).get("parse_confidence") or 0.8
    )
    for key_name in ("title", "drawing_number", "level", "scale_ratio"):
        val = label_data.get("meta", {}).get(key_name)
        if val:
            meta[key_name] = val

    graph = build_graph(
        regions, label_data, meta, fixture_regions=fixture_regions, handwriting=handwriting
    )

    try:
        validate(graph)
        return graph
    except Exception as first_err:
        graph["meta"].setdefault("warnings", []).append(
            f"validation_repair: {first_err}"
        )
        # Re-label once with the error hint, then rebuild.
        from src.secrets_util import get_anthropic_key
        import anthropic

        client = anthropic.Anthropic(api_key=get_anthropic_key())
        system = PROMPT_MARKS.read_text(encoding="utf-8")
        schema = _labels_schema()
        b64, media_type = _encode_image_bgr(marked)
        repair = f"Previous output failed validation: {first_err}. Fix categories/labels and retry."
        response = _call_claude(client, model, system, schema, b64, media_type, extra_text=repair)
        label_data = _extract_json(response)
        label_data.setdefault("meta", {})["source_file"] = source_file
        graph = build_graph(
            regions, label_data, meta, fixture_regions=fixture_regions, handwriting=handwriting
        )
        validate(graph)
        return graph


def main():
    parser = argparse.ArgumentParser(description="Parse a floor plan (Roboflow + Claude CV-hybrid)")
    parser.add_argument("image", nargs="?", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--debug-marks", default=None, help="Save numbered marks image")
    parser.add_argument("--conf", type=float, default=DEFAULT_CONF, help="Roboflow confidence threshold")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--handwriting", action="store_true", help="OCR handwritten/red-ink notes into annotations[]")
    args = parser.parse_args()

    if not args.image:
        print("Provide image path")
        return

    graph = parse_floorplan_hybrid(
        args.image, conf=args.conf, model=args.model,
        debug_marks_path=args.debug_marks, handwriting=args.handwriting,
    )
    out = Path(args.out) if args.out else ROOT / "data" / f"parsed_floor_{Path(args.image).stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    print(
        "Wrote", out,
        "| parser:", graph["meta"].get("parser"),
        "| spaces:", len(graph["spaces"]),
    )


if __name__ == "__main__":
    main()
