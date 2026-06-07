"""
Datacenter CV-hybrid parser: OpenCV detects pixel-accurate boxes;
Claude labels numbered marks (Set-of-Marks). No coordinates from Claude.

Styles: serverroom | datahall | cooling
"""

import argparse
import base64
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMAS = {
    "serverroom": ROOT / "schemas" / "dc_serverroom.schema.json",
    "datahall": ROOT / "schemas" / "dc_datahall.schema.json",
    "cooling": ROOT / "schemas" / "cooling_pid.schema.json",
}
PROMPTS = {
    "serverroom": ROOT / "prompts" / "system_dc_serverroom.md",
    "datahall": ROOT / "prompts" / "system_dc_datahall.md",
    "cooling": ROOT / "prompts" / "system_cooling_pid.md",
}
STYLE_FILES = {
    "data/datacenter/preview.webp": "datahall",
    "preview.webp": "datahall",
    "preview (2).webp": "serverroom",
    "preview (1).webp": "cooling",
}
MIN_REGIONS = {"serverroom": 5, "datahall": 6, "cooling": 4}
DIAGRAM_TYPES = {
    "serverroom": "DC_SERVERROOM",
    "datahall": "DC_DATAHALL",
    "cooling": "COOLING_PID",
}
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_EDGE = 1568
SQFT_TO_M2 = 0.092903

VALID_ENUMS = {
    "serverroom": {
        "zone_cat": {"aisle", "work_zone", "storage_zone", "unknown"},
        "eq_type": {"rack", "aircon", "shelving", "cart", "cabinet", "post", "unknown"},
    },
    "datahall": {
        "space_cat": {
            "data_hall", "raised_floor", "hot_aisle", "cold_aisle",
            "electrical_room", "ups_room", "noc", "security", "unknown",
        },
        "node_type": {"rack", "ups", "switchgear", "pdu", "mdp", "crac", "unknown"},
        "edge_type": {
            "chw_supply", "chw_return", "condenser_water", "air_duct",
            "electrical_cable", "control_signal", "unknown",
        },
    },
    "cooling": {
        "space_cat": {"data_hall", "electrical_room", "unknown"},
        "node_type": {
            "chiller", "cooling_tower", "pump", "pdu", "ups", "transformer",
            "ac_dc", "dc_dc", "server_room", "unknown",
        },
        "edge_type": {
            "chw_supply", "chw_return", "condenser_water", "air_duct",
            "electrical_cable", "control_signal", "unknown",
        },
    },
}


def _pick_enum(value, allowed, default="unknown"):
    v = (value or default).lower().replace(" ", "_").replace("-", "_")
    return v if v in allowed else default


def _label_attributes(lab):
    """Pull numeric ratings from flat label fields into attributes dict."""
    attrs = {}
    for key in ("rated_power_kW", "ampacity_A", "tonnage_RT", "rack_count"):
        val = lab.get(key)
        if val is not None and val != 0:
            attrs[key] = val
    raw = lab.get("attributes")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v is not None and v != 0:
                attrs[k] = v
    return attrs


def _labels_schema(style):
    """Claude structured-output schema for set-of-marks labeling."""
    label_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "kind", "name", "confidence"],
        "properties": {
            "index": {"type": "integer"},
            "kind": {"type": "string"},
            "name": {"type": "string"},
            "category": {"type": "string"},
            "type": {"type": "string"},
            "tag": {"type": "string"},
            "area_sqft": {"type": "number"},
            "rated_power_kW": {"type": "number"},
            "ampacity_A": {"type": "number"},
            "tonnage_RT": {"type": "number"},
            "confidence": {"type": "number"},
        },
    }
    props = {
        "meta": {
            "type": "object",
            "additionalProperties": False,
            "required": ["diagram_id", "diagram_type", "source_file", "parse_confidence"],
            "properties": {
                "diagram_id": {"type": "string"},
                "diagram_type": {"type": "string"},
                "source_file": {"type": "string"},
                "parse_confidence": {"type": "number"},
            },
        },
        "labels": {"type": "array", "items": label_item},
    }

    from src import cv_marks as marks
    props["handwriting"] = {"type": "array", "items": marks.handwriting_schema_item()}
    required = ["meta", "labels"]
    if style == "cooling":
        props["edges"] = {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "type", "from", "to", "confidence"],
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string"},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        }
        required.append("edges")
    return {"type": "object", "additionalProperties": False, "required": required, "properties": props}


def load_dc_image(path):
    """Load PNG/JPG/WEBP; PIL fallback when cv2 cannot read webp."""
    path = Path(path)
    img = cv2.imread(str(path))
    if img is not None:
        return img, path.name
    try:
        from PIL import Image

        pil = Image.open(path).convert("RGB")
        arr = np.array(pil)
        img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        return img, path.name
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


def _dedupe_regions(regions, iou_thresh=0.45):
    """Keep larger box when two regions overlap heavily."""
    if not regions:
        return regions
    order = sorted(regions, key=lambda r: r["area_px"], reverse=True)
    kept = []
    for reg in order:
        if any(_iou(reg["bbox_px"], k["bbox_px"]) > iou_thresh for k in kept):
            continue
        kept.append(reg)
    return kept


def _contours_to_regions(contours, h, w, img_area, min_area_frac, kind_hint_fn):
    """Convert contours to region dicts with filtering."""
    regions = []
    margin = 0.02
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < img_area * min_area_frac or area > img_area * 0.75:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw < 12 or bh < 12:
            continue
        rect_area = bw * bh
        if rect_area <= 0 or area / rect_area < 0.08:
            continue
        # skip near-full-page border boxes
        if x <= w * margin and y <= h * margin and (x + bw) >= w * (1 - margin) and (y + bh) >= h * (1 - margin):
            continue

        epsilon = 0.015 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        poly_px = [[float(p[0][0]), float(p[0][1])] for p in approx]
        if len(poly_px) < 3:
            poly_px = [[x, y], [x + bw, y], [x + bw, y + bh], [x, y + bh]]

        cx = (x + bw / 2) / w
        cy = (y + bh / 2) / h
        kind_hint = kind_hint_fn(area, img_area)

        regions.append(
            {
                "index": 0,
                "kind_hint": kind_hint,
                "bbox_px": [x, y, x + bw, y + bh],
                "bbox_2d": [round(cx, 5), round(cy, 5), round(bw / w, 5), round(bh / h, 5)],
                "polygon_2d": [[round(p[0] / w, 5), round(p[1] / h, 5)] for p in poly_px],
                "area_px": area,
            }
        )
    return regions


def detect_regions(image, min_area_frac=0.0008, max_regions=45):
    """
    Detect rectangular regions via multiple CV passes (tree contours,
    gray fills, color aisles). Returns pixel-accurate boxes for Set-of-Marks.
    """
    h, w = image.shape[:2]
    img_area = h * w
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

    def kind_fn(area, total):
        return "zone" if area > total * 0.03 else "equipment"

    all_regions = []

    # Pass 1: RETR_TREE on adaptive threshold (captures nested room boxes)
    adapt = cv2.adaptiveThreshold(
        blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 3
    )
    adapt = cv2.morphologyEx(adapt, cv2.MORPH_CLOSE, kernel, iterations=1)
    contours, _ = cv2.findContours(adapt, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    all_regions.extend(_contours_to_regions(contours, h, w, img_area, min_area_frac, kind_fn))

    # Pass 2: Canny edges closed (line-drawn boxes)
    edges = cv2.Canny(blur, 30, 100)
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    all_regions.extend(_contours_to_regions(contours, h, w, img_area, min_area_frac, kind_fn))

    # Pass 3: gray fill detection (server room equipment blocks)
    _, gray_bin = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    gray_bin = cv2.morphologyEx(gray_bin, cv2.MORPH_OPEN, kernel, iterations=1)
    contours, _ = cv2.findContours(gray_bin, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    all_regions.extend(_contours_to_regions(contours, h, w, img_area, min_area_frac * 0.5, kind_fn))

    # Pass 4: saturated color regions (data hall hot/cold aisles)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    sat_mask = cv2.threshold(hsv[:, :, 1], 30, 255, cv2.THRESH_BINARY)[1]
    sat_mask = cv2.morphologyEx(sat_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(sat_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    all_regions.extend(_contours_to_regions(contours, h, w, img_area, min_area_frac, kind_fn))

    # Pass 5: MSER-like small rectangles via morph gradient
    grad = cv2.morphologyEx(blur, cv2.MORPH_GRADIENT, kernel)
    _, grad_bin = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    grad_bin = cv2.morphologyEx(grad_bin, cv2.MORPH_CLOSE, kernel, iterations=1)
    contours, _ = cv2.findContours(grad_bin, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    all_regions.extend(_contours_to_regions(contours, h, w, img_area, min_area_frac * 0.3, kind_fn))

    regions = _dedupe_regions(all_regions, iou_thresh=0.35)
    regions.sort(key=lambda r: (r["bbox_2d"][1], r["bbox_2d"][0]))
    regions = regions[:max_regions]
    for i, reg in enumerate(regions):
        reg["index"] = i + 1
    return regions


def draw_marks(image, regions):
    """Draw numbered red boxes; return annotated copy."""
    out = image.copy()
    h, w = out.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    for reg in regions:
        x1, y1, x2, y2 = [int(v) for v in reg["bbox_px"]]
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255), 2)
        label = str(reg["index"])
        scale = max(0.4, min(1.2, (x2 - x1) / 80))
        (tw, th), _ = cv2.getTextSize(label, font, scale, 2)
        tx = x1 + 4
        ty = y1 + th + 6
        cv2.rectangle(out, (tx - 2, ty - th - 4), (tx + tw + 4, ty + 4), (255, 255, 255), -1)
        cv2.putText(out, label, (tx, ty), font, scale, (0, 0, 200), 2, cv2.LINE_AA)
    return out


def _encode_image_bgr(img, max_edge=MAX_EDGE):
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side > max_edge:
        scale = max_edge / long_side
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("Failed to encode image")
    return base64.standard_b64encode(buf.tobytes()).decode("utf-8"), "image/png", h, w


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
                    {
                        "type": "text",
                        "text": extra_text or "Label every numbered box in the image.",
                    },
                ],
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )


def _extract_json(response):
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise RuntimeError("Claude response truncated (max_tokens).")
    parts = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    raw = "".join(parts).strip()
    return json.loads(raw)


def label_regions(image_marked, regions, style, source_file, model=DEFAULT_MODEL, handwriting=False):
    """Claude labels each numbered region; one repair retry."""
    from src.secrets_util import get_anthropic_key

    key = get_anthropic_key()
    if not key:
        raise FileNotFoundError(
            "No Anthropic API key (set ANTHROPIC_API_KEY or models/secretapi.txt)"
        )
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    system = PROMPTS[style].read_text(encoding="utf-8")
    schema = _labels_schema(style)
    b64, media_type, _, _ = _encode_image_bgr(image_marked)
    indices = ", ".join(str(r["index"]) for r in regions)

    from src import cv_marks as marks
    extra = f"Label boxes numbered: {indices}. Return one label entry per box index."
    hw = marks.handwriting_extra_text(handwriting)
    if hw:
        extra = f"{extra}\n\n{hw}"

    response = _call_claude(client, model, system, schema, b64, media_type, extra_text=extra)
    data = _extract_json(response)
    data.setdefault("meta", {})
    data["meta"]["source_file"] = source_file
    return data


def _poly_area_norm(poly):
    if len(poly) < 3:
        return 0.0
    area = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _assign_container(point, containers, id_key="id"):
    """Return id of smallest container polygon holding point."""
    try:
        from shapely.geometry import Point, Polygon
    except ImportError:
        return None

    pt = Point(point)
    best_id = None
    best_area = None
    for c in containers:
        poly_pts = c.get("polygon_2d") or []
        if len(poly_pts) < 3:
            continue
        try:
            poly = Polygon(poly_pts)
            if poly.contains(pt) or poly.distance(pt) < 0.02:
                a = poly.area
                if best_area is None or a < best_area:
                    best_area = a
                    best_id = c[id_key]
        except Exception:
            continue
    return best_id


def _slug(s, fallback="item"):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", (s or fallback).strip())[:30]
    return s.strip("_") or fallback


def build_graph(regions, label_data, style, meta, handwriting=False):
    """Merge CV geometry with Claude labels into per-style graph."""
    label_map = {lab["index"]: lab for lab in label_data.get("labels") or []}
    warnings = list(meta.get("warnings") or [])

    if style == "serverroom":
        zones, equipment = [], []
        z_i, e_i = 0, 0
        for reg in regions:
            lab = label_map.get(reg["index"], {})
            kind = (lab.get("kind") or reg["kind_hint"]).lower()
            name = lab.get("name") or lab.get("tag") or f"Region {reg['index']}"
            conf = float(lab.get("confidence") or 0.75)
            area_norm = _poly_area_norm(reg["polygon_2d"])
            area_m2 = round(area_norm * meta["image_width"] * meta["image_height"] * 1e-6, 2)
            area_sqft = lab.get("area_sqft")
            if area_sqft:
                area_m2 = round(float(area_sqft) * SQFT_TO_M2, 2)

            if kind in ("zone", "space") or reg["kind_hint"] == "zone":
                z_i += 1
                zone = {
                    "id": f"Z{z_i}",
                    "name": name,
                    "category": _pick_enum(
                        lab.get("category"), VALID_ENUMS["serverroom"]["zone_cat"]
                    ),
                    "bbox_2d": reg["bbox_2d"],
                    "polygon_2d": reg["polygon_2d"],
                    "area_m2": area_m2,
                    "confidence": conf,
                }
                if area_sqft is not None:
                    zone["area_sqft"] = float(area_sqft)
                zones.append(zone)
            else:
                e_i += 1
                eq = {
                    "id": f"E{e_i}",
                    "type": _pick_enum(lab.get("type"), VALID_ENUMS["serverroom"]["eq_type"]),
                    "tag": lab.get("tag") or name,
                    "bbox_2d": reg["bbox_2d"],
                    "zone_id": None,
                    "confidence": conf,
                }
                attrs = _label_attributes(lab)
                if attrs:
                    eq["attributes"] = attrs
                equipment.append(eq)

        for eq in equipment:
            cx, cy = eq["bbox_2d"][0], eq["bbox_2d"][1]
            eq["zone_id"] = _assign_container((cx, cy), zones, id_key="id")

        total_m2 = round(sum(z.get("area_m2") or 0 for z in zones), 1)
        graph = {
            "meta": {
                **meta,
                "diagram_type": "DC_SERVERROOM",
                "room_count": len(zones),
                "total_area_m2": total_m2,
                "warnings": warnings,
            },
            "zones": zones,
            "equipment": equipment,
        }

        __import__('src.cv_marks', fromlist=['']).attach_handwriting_annotations(
            graph, label_data, enabled=handwriting
        )
        return graph

    if style == "datahall":
        spaces, nodes = [], []
        s_i, n_i = 0, 0
        for reg in regions:
            lab = label_map.get(reg["index"], {})
            kind = (lab.get("kind") or reg["kind_hint"]).lower()
            name = lab.get("name") or lab.get("tag") or f"Region {reg['index']}"
            conf = float(lab.get("confidence") or 0.75)
            area_norm = _poly_area_norm(reg["polygon_2d"])
            area_m2 = round(area_norm * meta["image_width"] * meta["image_height"] * 1e-6, 2)

            if kind in ("space", "zone") or reg["kind_hint"] == "zone":
                s_i += 1
                spaces.append(
                    {
                        "id": f"S{s_i}",
                        "name": name,
                        "category": _pick_enum(
                            lab.get("category"), VALID_ENUMS["datahall"]["space_cat"]
                        ),
                        "bbox_2d": reg["bbox_2d"],
                        "polygon_2d": reg["polygon_2d"],
                        "area_m2": max(area_m2, 0.1),
                        "confidence": conf,
                    }
                )
            else:
                n_i += 1
                node = {
                    "id": f"N{n_i}",
                    "type": _pick_enum(lab.get("type"), VALID_ENUMS["datahall"]["node_type"]),
                    "tag": lab.get("tag") or name,
                    "space_id": None,
                    "bbox_2d": reg["bbox_2d"],
                    "confidence": conf,
                }
                attrs = _label_attributes(lab)
                if attrs:
                    node["attributes"] = attrs
                nodes.append(node)

        for node in nodes:
            cx, cy = node["bbox_2d"][0], node["bbox_2d"][1]
            node["space_id"] = _assign_container((cx, cy), spaces, id_key="id")

        graph = {
            "meta": {
                **meta,
                "diagram_type": "DC_DATAHALL",
                "room_count": len(spaces),
                "total_floor_area_m2": round(sum(s.get("area_m2") or 0 for s in spaces), 1),
                "warnings": warnings,
            },
            "spaces": spaces,
            "nodes": nodes,
            "edges": [],
        }

        __import__('src.cv_marks', fromlist=['']).attach_handwriting_annotations(
            graph, label_data, enabled=handwriting
        )
        return graph

    # cooling
    spaces, nodes = [], []
    s_i, n_i = 0, 0
    tag_to_id = {}
    for reg in regions:
        lab = label_map.get(reg["index"], {})
        kind = (lab.get("kind") or "node").lower()
        name = lab.get("name") or lab.get("tag") or f"Region {reg['index']}"
        conf = float(lab.get("confidence") or 0.75)
        area_norm = _poly_area_norm(reg["polygon_2d"])
        area_m2 = round(area_norm * meta["image_width"] * meta["image_height"] * 1e-6, 2)

        if kind == "space":
            s_i += 1
            sid = f"SP{s_i}"
            spaces.append(
                {
                    "id": sid,
                    "name": name,
                    "category": _pick_enum(
                        lab.get("category"), VALID_ENUMS["cooling"]["space_cat"]
                    ),
                    "bbox_2d": reg["bbox_2d"],
                    "polygon_2d": reg["polygon_2d"],
                    "area_m2": max(area_m2, 0.1),
                    "confidence": conf,
                }
            )
            tag_to_id[lab.get("tag") or name] = sid
        else:
            n_i += 1
            nid = f"N{n_i}"
            tag = lab.get("tag") or name
            node = {
                "id": nid,
                "type": _pick_enum(lab.get("type"), VALID_ENUMS["cooling"]["node_type"]),
                "tag": tag,
                "bbox_2d": reg["bbox_2d"],
                "confidence": conf,
            }
            attrs = _label_attributes(lab)
            if attrs:
                node["attributes"] = attrs
            nodes.append(node)
            tag_to_id[tag] = nid
            tag_to_id[name] = nid

    edges = []
    for i, edge in enumerate(label_data.get("edges") or []):
        efrom = edge.get("from", "")
        eto = edge.get("to", "")
        fid = tag_to_id.get(efrom) or _slug(efrom, f"from{i}")
        tid = tag_to_id.get(eto) or _slug(eto, f"to{i}")
        edges.append(
            {
                "id": edge.get("id") or f"E{i + 1}",
                "type": _pick_enum(edge.get("type"), VALID_ENUMS["cooling"]["edge_type"]),
                "from": fid,
                "to": tid,
                "confidence": float(edge.get("confidence") or 0.7),
            }
        )

    graph = {
        "meta": {**meta, "diagram_type": "COOLING_PID", "warnings": warnings},
        "spaces": spaces,
        "nodes": nodes,
        "edges": edges,
    }

    __import__('src.cv_marks', fromlist=['']).attach_handwriting_annotations(
        graph, label_data, enabled=handwriting
    )
    return graph


def validate(graph, style):
    import jsonschema

    with open(SCHEMAS[style], "r", encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(graph, schema)
    return True


def detect_style(path, hint="auto"):
    if hint and hint != "auto":
        return hint
    name = Path(path).name.lower()
    for key, style in STYLE_FILES.items():
        if key.lower() in name or name == Path(key).name.lower():
            return style
    if "server" in name or "(2)" in name:
        return "serverroom"
    if "cool" in name or "(1)" in name:
        return "cooling"
    return "datahall"


def _fallback_claude_parse(path, style, model, handwriting=False):
    """Claude-only coordinate parser when CV finds too few regions."""

    from src import claude_parse as claude_mod
    diagram_type = "PID" if style == "cooling" else "FLOORPLAN"
    graph = claude_mod.parse_with_claude(
        path, diagram_type=diagram_type, model=model, handwriting=handwriting
    )
    graph.setdefault("meta", {})
    graph["meta"]["parser"] = "claude"
    graph["meta"]["warnings"] = list(graph["meta"].get("warnings") or []) + [
        f"cv_hybrid_fallback: too few CV regions for {style}; used Claude coordinate parser"
    ]
    return graph


def parse_datacenter(path, style="auto", model=DEFAULT_MODEL, debug_marks_path=None, handwriting=False):
    """
    Full CV-hybrid pipeline. Returns graph dict validated against per-style schema.
    """
    style = detect_style(path, style)
    image, source_file = load_dc_image(path)
    h, w = image.shape[:2]

    regions = detect_regions(image)
    min_req = MIN_REGIONS[style]
    if len(regions) < min_req:
        regions = detect_regions(image, min_area_frac=0.0008, max_regions=60)

    meta = {
        "diagram_id": f"dc-{style}",
        "diagram_type": DIAGRAM_TYPES[style],
        "source_file": source_file,
        "image_width": w,
        "image_height": h,
        "parse_confidence": 0.0,
        "parser": "cv-hybrid",
        "topology": "set_of_marks",
        "warnings": [],
    }

    if len(regions) < min_req:
        meta["warnings"].append(f"cv_regions_low: found {len(regions)}, need {min_req}")
        return _fallback_claude_parse(path, style, model, handwriting=handwriting)

    marked = draw_marks(image, regions)
    if debug_marks_path:
        cv2.imwrite(str(debug_marks_path), marked)

    label_data = label_regions(
        marked, regions, style, source_file, model=model, handwriting=handwriting
    )
    meta["parse_confidence"] = float(
        label_data.get("meta", {}).get("parse_confidence") or 0.8
    )

    graph = build_graph(regions, label_data, style, meta, handwriting=handwriting)

    try:
        validate(graph, style)
        return graph
    except Exception as first_err:
        repair_data = label_data
        repair_data.setdefault("meta", {})["_repair"] = str(first_err)
        # one retry: re-label with validation error hint
        from src.secrets_util import get_anthropic_key
        import anthropic

        key = get_anthropic_key()
        client = anthropic.Anthropic(api_key=key)
        system = PROMPTS[style].read_text(encoding="utf-8")
        schema = _labels_schema(style)
        b64, media_type, _, _ = _encode_image_bgr(marked)
        repair = f"Previous output failed validation: {first_err}. Fix labels/categories and retry."
        response = _call_claude(client, model, system, schema, b64, media_type, extra_text=repair)
        label_data = _extract_json(response)
        graph = build_graph(regions, label_data, style, meta, handwriting=handwriting)
        validate(graph, style)
        return graph


def main():
    parser = argparse.ArgumentParser(description="Parse datacenter plan (CV-hybrid)")
    parser.add_argument("image", nargs="?", default=None)
    parser.add_argument("--style", default="auto", choices=["auto", "serverroom", "datahall", "cooling"])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", default=None)
    parser.add_argument("--debug-marks", default=None)
    parser.add_argument("--handwriting", action="store_true", help="OCR handwritten/red-ink notes into annotations[]")
    args = parser.parse_args()

    if not args.image:
        print("Provide image path")
        return

    graph = parse_datacenter(
        args.image,
        style=args.style,
        model=args.model,
        debug_marks_path=args.debug_marks,
        handwriting=args.handwriting,
    )
    style = detect_style(args.image, args.style)
    out = Path(args.out) if args.out else ROOT / "data" / f"parsed_dc_{style}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    print("Wrote", out, "| parser:", graph["meta"].get("parser"))


if __name__ == "__main__":
    main()
