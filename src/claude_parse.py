"""
Claude VLM parser: image -> unified graph v2 JSON (primary parser).

Reads symbols, dimensions, handwriting, and nameplate ratings.
Validates against the strict schemas/graph.schema.json; 1 repair retry.
Raises on any failure so the caller (06_fusion) can fall back to YOLO/OpenCV.
"""

import argparse
import base64
import json
import math
import re
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STRICT_SCHEMA = ROOT / "schemas" / "graph.schema.json"
CLAUDE_SCHEMA = ROOT / "schemas" / "claude_output.schema.json"
PROMPT_FLOORPLAN = ROOT / "prompts" / "system_floorplan.md"
PROMPT_PID_SLD = ROOT / "prompts" / "system_pid_sld.md"

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_EDGE = 1568

FT_M = 0.3048
IN_M = 0.0254
SQFT_M2 = 0.092903
_IMPERIAL_HINTS = ("sq ft", "sqft", " sf", "ft2", "ft\u00b2", "square feet", "'", "\"")


def _enhance_for_vision(img):
    """
    Boost legibility of scanned line drawings before sending to the VLM:
    CLAHE on luminance + light denoise. Strengthens faint walls, thin
    pipes, and small dimension text without distorting geometry.
    """
    try:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        enhanced = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
        return cv2.bilateralFilter(enhanced, d=5, sigmaColor=40, sigmaSpace=40)
    except Exception:
        return img


def _encode_image(path, enhance=True):
    """Resize longest edge to ~1568px (never upscale), return (base64, media_type, (h, w))."""
    img = cv2.imread(str(path))
    if img is None:
        # PDF or unreadable by cv2 — reuse preprocess loader

        from src import preprocess as prep
        img, _ = prep.load_image(path, max_edge=MAX_EDGE)
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side > MAX_EDGE:
        scale = MAX_EDGE / long_side
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    if enhance:
        img = _enhance_for_vision(img)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("Failed to encode image")
    return base64.standard_b64encode(buf.tobytes()).decode("utf-8"), "image/png", img.shape[:2]


def _system_prompt(diagram_type):
    path = PROMPT_FLOORPLAN if diagram_type == "FLOORPLAN" else PROMPT_PID_SLD
    return path.read_text(encoding="utf-8")


def _load_claude_schema():
    with open(CLAUDE_SCHEMA, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Measurement parsing
# ---------------------------------------------------------------------------
def _to_float(s):
    """Parse '11,20' / '11.20' / '1 575' / '70,0 m2' -> float, tolerant of separators."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace("\u00a0", " ").strip()
    m = re.search(r"-?\d[\d \.,]*", t)
    if not m:
        return None
    num = m.group(0).strip().replace(" ", "")
    if "," in num and "." in num:           # 1.234,56 -> 1234.56
        num = num.replace(".", "").replace(",", ".")
    elif "," in num:                        # 11,20 -> 11.20
        num = num.replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def _parse_imperial_len(token):
    """\"18'3\\\"\" / \"20'0'\" / \"15'\" -> metres."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*'\s*(\d+(?:\.\d+)?)?", str(token))
    if not m:
        return None
    feet = float(m.group(1))
    inch = float(m.group(2)) if m.group(2) else 0.0
    return feet * FT_M + inch * IN_M


def _parse_dimensions(raw):
    """'18\\'3\"x15\\'1\"' or '3.50 x 4.10' -> (width_m, height_m)."""
    if not raw:
        return None
    s = str(raw).replace("\u00d7", "x").replace("X", "x")
    parts = [p.strip() for p in s.split("x")]
    if len(parts) != 2:
        return None
    if "'" in s or "\"" in s:
        a, b = _parse_imperial_len(parts[0]), _parse_imperial_len(parts[1])
    else:                                   # bare numbers -> assume metres
        a, b = _to_float(parts[0]), _to_float(parts[1])
    if a and b:
        return round(a, 3), round(b, 3)
    return None


def _parse_area_to_m2(raw):
    """'11,20 m2' -> 11.2 ; '465 SQ FT' -> 43.2 ; None if unparseable."""
    if not raw:
        return None
    s = str(raw).lower()
    val = _to_float(s)
    if val is None:
        return None
    if any(h in s for h in ("sq ft", "sqft", " sf", "ft2", "ft\u00b2", "square feet")):
        return round(val * SQFT_M2, 2)
    return round(val, 2)                     # assume m2


def _is_imperial_text(*texts):
    blob = " ".join(t for t in texts if t).lower()
    return any(h in blob for h in _IMPERIAL_HINTS)


def _shoelace_norm(poly):
    """Polygon area in normalized^2 units (0..1)."""
    pts = [p for p in poly if len(p) >= 2]
    if len(pts) < 3:
        return 0.0
    acc = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        acc += x1 * y2 - x2 * y1
    return abs(acc) / 2.0


def _drop_empty_values(attrs):
    """Remove null/zero placeholders Claude uses for N/A ratings."""
    out = {}
    for k, v in (attrs or {}).items():
        if v is None:
            continue
        if isinstance(v, (int, float)) and v == 0:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        out[k] = v
    return out


def _normalize_to_strict(graph):
    """Convert Claude flat attributes -> strict nested capacity/efficiency objects."""
    for node in graph.get("nodes") or []:
        attrs = _drop_empty_values(node.get("attributes") or {})
        cop = attrs.pop("cop", None)
        cap_v = attrs.pop("capacity_value", None)
        cap_u = attrs.pop("capacity_unit", None)
        if cop is not None:
            attrs["efficiency"] = {"metric": "COP", "value": cop}
        if cap_v is not None and cap_u:
            attrs["capacity"] = {"value": cap_v, "unit": cap_u}
        node["attributes"] = attrs
        if not node.get("bbox_2d"):
            node["bbox_2d"] = [0.5, 0.5, 0.05, 0.05]
    for edge in graph.get("edges") or []:
        edge["attributes"] = _drop_empty_values(edge.get("attributes") or {})
    # Drop nullable optional fields Claude may emit as null (strict schema is
    # not nullable for these); `habitable` False is meaningful so only drop None.
    space_nullable = (
        "it_power_kW", "elevation_m", "area_raw", "dimensions_raw",
        "room_number", "habitable", "area_source", "width_m", "height_m", "floor",
    )
    for sp in graph.get("spaces") or []:
        for k in space_nullable:
            if sp.get(k) is None:
                sp.pop(k, None)
    # Same for nullable optional meta strings/enums.
    meta = graph.get("meta") or {}
    for k in ("title", "drawing_number", "level", "unit_system",
              "scale_ratio", "scale_source", "total_floor_area_source"):
        if meta.get(k) is None:
            meta.pop(k, None)
    # strict schema requires nodes minItems>=1 only for MEP; floor plans may have none.
    return graph


def _bbox_from_polygon(poly):
    """[[x,y],...] normalized -> [x_center, y_center, width, height] normalized."""
    xs = [p[0] for p in poly if len(p) >= 2]
    ys = [p[1] for p in poly if len(p) >= 2]
    if not xs or not ys:
        return None
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    return [
        round((x_min + x_max) / 2, 5),
        round((y_min + y_max) / 2, 5),
        round(x_max - x_min, 5),
        round(y_max - y_min, 5),
    ]


def _enrich_spaces(graph):
    """Derive bbox, areas (annotated + scaled), width/height, scale, counts."""
    spaces = graph.get("spaces") or []
    meta = graph.setdefault("meta", {})
    img_w = meta.get("image_width")
    img_h = meta.get("image_height")

    # Pass 1: bbox, centroid, and trustworthy annotated measurements.
    for sp in spaces:
        poly = sp.get("polygon_2d") or []
        bbox = _bbox_from_polygon(poly)
        if bbox:
            sp["bbox_2d"] = bbox
            sp["centroid_2d"] = [bbox[0], bbox[1]]

        # parse a verbatim dimension string into width/height (metres)
        if sp.get("dimensions_raw") and not (sp.get("width_m") and sp.get("height_m")):
            dims = _parse_dimensions(sp["dimensions_raw"])
            if dims:
                sp["width_m"], sp["height_m"] = dims

        # establish area_m2 from annotation if not already numeric
        if sp.get("area_m2") is None:
            from_raw = _parse_area_to_m2(sp.get("area_raw"))
            if from_raw is not None:
                sp["area_m2"] = from_raw
                sp["area_source"] = "annotated"
            elif sp.get("width_m") and sp.get("height_m"):
                sp["area_m2"] = round(sp["width_m"] * sp["height_m"], 2)
                sp["area_source"] = "annotated"
        elif not sp.get("area_source"):
            sp["area_source"] = "annotated"

    # infer unit system from any verbatim measurement text
    if not meta.get("unit_system"):
        texts = [t for sp in spaces for t in (sp.get("area_raw"), sp.get("dimensions_raw"))]
        meta["unit_system"] = "imperial" if _is_imperial_text(*texts) else "metric"

    # Pass 2: derive a real scale (m/px) -- only once, then reuse.
    scale = meta.get("scale_m_per_px")
    if not scale and img_w and img_h:
        scale = _derive_scale(spaces, img_w, img_h)
        if scale:
            meta["scale_m_per_px"] = round(scale, 6)
            meta.setdefault("scale_source", "annotation")

    # Pass 3: fill missing areas / dimensions from the scale.
    if scale and scale > 0 and img_w and img_h:
        px_area = img_w * img_h
        for sp in spaces:
            bbox = sp.get("bbox_2d")
            poly = sp.get("polygon_2d") or []
            if sp.get("area_source") != "annotated" and poly:
                norm_a = _shoelace_norm(poly)
                if norm_a > 0:
                    sp["area_m2"] = round(norm_a * px_area * scale * scale, 2)
                    sp["area_source"] = "derived_scale"
            if bbox:
                if not sp.get("width_m"):
                    sp["width_m"] = round(bbox[2] * img_w * scale, 2)
                if not sp.get("height_m"):
                    sp["height_m"] = round(bbox[3] * img_h * scale, 2)

    # Counts + total (annotated rooms summed where possible).
    meta["room_count"] = len(spaces)
    areas = [float(s["area_m2"]) for s in spaces if s.get("area_m2") is not None]
    if areas:
        meta["total_floor_area_m2"] = round(sum(areas), 1)
        all_annotated = bool(spaces) and all(
            s.get("area_source") == "annotated" for s in spaces if s.get("area_m2") is not None
        )
        meta["total_floor_area_source"] = "summed_rooms" if all_annotated else "derived"
    return graph


def _derive_scale(spaces, img_w, img_h):
    """
    Estimate metres-per-pixel. Most reliable signal on a floor plan is an
    annotated room area: scale = sqrt(area_m2 / area_px). Median over all
    annotated rooms; falls back to an annotated room width.
    """
    px_area = img_w * img_h
    estimates = []
    for sp in spaces:
        if sp.get("area_source") == "annotated" and sp.get("area_m2"):
            norm_a = _shoelace_norm(sp.get("polygon_2d") or [])
            if norm_a > 0:
                area_px = norm_a * px_area
                if area_px > 0:
                    estimates.append(math.sqrt(float(sp["area_m2"]) / area_px))
    if estimates:
        estimates.sort()
        return estimates[len(estimates) // 2]

    for sp in spaces:                        # fallback: a labelled width
        bbox = sp.get("bbox_2d")
        if sp.get("width_m") and bbox:
            bbox_w_px = bbox[2] * img_w
            if bbox_w_px > 0:
                return sp["width_m"] / bbox_w_px
    return None


def _snap_axis(values, tol):
    """Cluster 1-D coords within tol; return {value: cluster_representative}."""
    if not values:
        return {}
    ordered = sorted(set(values))
    clusters = [[ordered[0]]]
    for v in ordered[1:]:
        if v - clusters[-1][-1] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    mapping = {}
    for cluster in clusters:
        rep = round(sum(cluster) / len(cluster), 5)
        for v in cluster:
            mapping[v] = rep
    return mapping


def _snap_vertices(graph, tol=0.012):
    """
    Align room polygon vertices to shared grid lines so adjacent rooms share
    identical wall coordinates. Closes small gaps and removes minor overlaps
    that come from the VLM eyeballing coordinates on a coarse grid.
    """
    spaces = graph.get("spaces") or []
    if not spaces:
        return graph

    xs, ys = [], []
    for sp in spaces:
        for pt in sp.get("polygon_2d") or []:
            if len(pt) >= 2:
                xs.append(pt[0])
                ys.append(pt[1])
    if not xs:
        return graph

    x_map = _snap_axis(xs, tol)
    y_map = _snap_axis(ys, tol)

    for sp in spaces:
        poly = sp.get("polygon_2d") or []
        snapped = []
        for pt in poly:
            if len(pt) >= 2:
                snapped.append([x_map.get(pt[0], pt[0]), y_map.get(pt[1], pt[1])])
        # drop consecutive duplicate vertices created by snapping
        deduped = []
        for pt in snapped:
            if not deduped or deduped[-1] != pt:
                deduped.append(pt)
        if len(deduped) > 1 and deduped[0] == deduped[-1]:
            deduped.pop()
        if len(deduped) >= 3:
            sp["polygon_2d"] = deduped
            bbox = _bbox_from_polygon(deduped)
            if bbox:
                sp["bbox_2d"] = bbox
    return graph


def _resolve_overlaps(graph):
    """Clip overlapping room polygons; higher-confidence rooms keep their area."""
    spaces = graph.get("spaces") or []
    if len(spaces) < 2:
        return graph
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except ImportError:
        return graph

    meta = graph.setdefault("meta", {})
    warnings = meta.setdefault("warnings", [])

    order = sorted(
        range(len(spaces)),
        key=lambda i: float(spaces[i].get("confidence") or 0),
        reverse=True,
    )

    accepted_geoms = []
    for idx in order:
        sp = spaces[idx]
        poly_pts = sp.get("polygon_2d") or []
        if len(poly_pts) < 3:
            continue
        try:
            poly = Polygon(poly_pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
        except Exception:
            continue
        if poly.is_empty or poly.area <= 0:
            continue

        if accepted_geoms:
            occupied = unary_union(accepted_geoms)
            if poly.intersects(occupied) and poly.intersection(occupied).area > 1e-9:
                clipped = poly.difference(occupied)
                if clipped.is_empty or clipped.area <= 1e-9:
                    accepted_geoms.append(poly)
                    continue
                if clipped.geom_type == "MultiPolygon":
                    clipped = max(clipped.geoms, key=lambda g: g.area)
                new_pts = [[round(x, 5), round(y, 5)] for x, y in clipped.exterior.coords[:-1]]
                if len(new_pts) >= 3:
                    sp["polygon_2d"] = new_pts
                    bbox = _bbox_from_polygon(new_pts)
                    if bbox:
                        sp["bbox_2d"] = bbox
                    warnings.append(
                        f"overlap_resolved: clipped room {sp.get('id')} "
                        f"({sp.get('name')}) to remove overlap"
                    )
                    poly = clipped
        accepted_geoms.append(poly)

    return graph


def _validate_strict(graph):
    import jsonschema

    with open(STRICT_SCHEMA, "r", encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(graph, schema)
    return True


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
                    {"type": "text", "text": extra_text or "Extract the graph JSON now."},
                ],
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )


def _extract_json(response):
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise RuntimeError(
            "Claude response truncated (max_tokens). Retry with a simpler diagram "
            "or fewer symbols; dense P&IDs are capped at ~30 nodes in the prompt."
        )
    parts = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    raw = "".join(parts).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from Claude ({exc}). Response may be truncated.") from exc


def _finalize(graph, path, w, h, diagram_type, handwriting=False):
    """Stamp meta, default arrays, normalize, enrich spaces, de-overlap."""

    handwriting_items = graph.pop("handwriting", None)
    graph.setdefault("meta", {})
    graph["meta"]["source_file"] = Path(path).name
    graph["meta"]["image_width"] = w
    graph["meta"]["image_height"] = h
    graph["meta"].setdefault("diagram_id", f"claude-{diagram_type.lower()}")
    graph["meta"]["parser"] = "claude"
    for k in ("spaces", "walls", "nodes", "edges"):
        graph.setdefault(k, [])
    graph = _normalize_to_strict(graph)
    graph = _enrich_spaces(graph)
    graph = _snap_vertices(graph)
    graph = _resolve_overlaps(graph)
    graph = _enrich_spaces(graph)
    if handwriting and handwriting_items:
        from src import cv_marks as marks
        marks.attach_handwriting_annotations(
            graph, {"handwriting": handwriting_items}, enabled=True
        )
    return graph


def parse_with_claude(path, diagram_type="PID", model=DEFAULT_MODEL, handwriting=False):
    """Parse an image with Claude. Raises on missing key / API / validation failure."""
    from src.secrets_util import get_anthropic_key

    key = get_anthropic_key()
    if not key:
        raise FileNotFoundError(
            "No Anthropic API key (set ANTHROPIC_API_KEY or models/secretapi.txt)"
        )

    import anthropic

    client = anthropic.Anthropic(api_key=key)
    b64, media_type, (h, w) = _encode_image(path)
    system = _system_prompt(diagram_type)
    schema = _load_claude_schema()


    from src import cv_marks as marks
    extra = marks.handwriting_extra_text(handwriting)
    response = _call_claude(client, model, system, schema, b64, media_type, extra_text=extra or "Extract the graph JSON now.")
    graph = _extract_json(response)
    graph = _finalize(graph, path, w, h, diagram_type, handwriting=handwriting)

    try:
        _validate_strict(graph)
        return graph
    except Exception as first_err:
        # one repair retry
        repair = (
            "Your previous JSON failed schema validation with error: "
            f"{first_err}. Return corrected JSON only, normalized 0-1 coordinates."
        )
        if extra:
            repair = f"{extra}\n\n{repair}"
        response = _call_claude(client, model, system, schema, b64, media_type, extra_text=repair)
        graph = _extract_json(response)
        graph = _finalize(graph, path, w, h, diagram_type, handwriting=handwriting)
        _validate_strict(graph)
        return graph


def main():
    parser = argparse.ArgumentParser(description="Parse a diagram with Claude vision")
    parser.add_argument("image")
    parser.add_argument("--type", default="PID", choices=["FLOORPLAN", "PID", "SLD"])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--out", default=None)
    parser.add_argument("--handwriting", action="store_true", help="OCR handwritten/red-ink notes into annotations[]")
    args = parser.parse_args()

    graph = parse_with_claude(
        args.image, diagram_type=args.type, model=args.model, handwriting=args.handwriting
    )
    out = Path(args.out) if args.out else ROOT / "data" / "parsed_claude.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    print(
        "Wrote", out,
        "| spaces:", len(graph["spaces"]),
        "nodes:", len(graph["nodes"]),
        "edges:", len(graph["edges"]),
    )


if __name__ == "__main__":
    main()
