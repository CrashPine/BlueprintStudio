"""
Shared set-of-marks helpers for the deterministic CV pipeline.

The detectors (Roboflow rooms/fixtures, local YOLO MEP symbols) own ALL
coordinates. These helpers:
  * draw_numbered_marks  -> annotate the image with numbered boxes,
  * stringify_detections -> turn detections into a text block for Claude,
  * derive_sizes         -> fill width_m/height_m from a known scale.

Claude never emits coordinates; it reads the numbered marks + this text block
and returns labels / tags / edges keyed by the provided integer index.
"""

import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Group colors (BGR) for the numbered marks image.
_GROUP_COLORS = {
    "space": (0, 0, 255),      # red boxes for rooms
    "opening": (255, 0, 0),    # blue for doors/windows
    "symbol": (0, 150, 0),     # green for MEP symbols
    "fixture": (200, 0, 200),  # magenta for sanitary/appliances
}


def _round_box(bbox, ndigits=4):
    return [round(float(v), ndigits) for v in bbox]


def draw_numbered_marks(image, regions, group_key="group"):
    """
    Draw a numbered box for each region; return the annotated copy.

    regions: list of dicts each with `index` (int), `bbox_px` ([x1,y1,x2,y2]),
             and optionally a `group` ("space"|"opening"|"symbol"|"fixture").
    """
    out = image.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    for reg in regions:
        x1, y1, x2, y2 = [int(v) for v in reg["bbox_px"]]
        color = _GROUP_COLORS.get(reg.get(group_key, "symbol"), (0, 150, 0))
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = str(reg["index"])
        scale = max(0.45, min(1.3, (x2 - x1) / 90.0))
        (tw, th), _ = cv2.getTextSize(label, font, scale, 2)
        tx, ty = x1 + 3, y1 + th + 5
        cv2.rectangle(out, (tx - 2, ty - th - 4), (tx + tw + 4, ty + 4), (255, 255, 255), -1)
        cv2.putText(out, label, (tx, ty), font, scale, color, 2, cv2.LINE_AA)
    return out


def stringify_detections(spaces=None, symbols=None, openings=None, fixtures=None):
    """
    Build the text block fed to Claude. Each entry is one line:
        [ID: <index>, Class: <class>, BBox: [cx, cy, w, h]]
    Coordinates are normalized 0-1 and MUST be reused verbatim by Claude.
    """
    sections = []

    def _section(title, regions):
        if not regions:
            return
        lines = [f"# {title}"]
        for reg in regions:
            cls = reg.get("class_hint") or reg.get("sub_type") or reg.get("class") or "unknown"
            bbox = _round_box(reg.get("bbox_2d") or [0, 0, 0, 0])
            lines.append(f"[ID: {reg['index']}, Class: {cls}, BBox: {bbox}]")
        sections.append("\n".join(lines))

    _section("DETECTED SPACES (rooms)", spaces)
    _section("DETECTED OPENINGS (doors/windows)", openings)
    _section("DETECTED FIXTURES (sanitary/appliances)", fixtures)
    _section("DETECTED SYMBOLS (MEP)", symbols)

    return "\n\n".join(sections)


def derive_sizes(instances, scale_m_per_px, img_w, img_h, bbox_key="bbox_2d"):
    """
    Fill width_m / height_m for each instance from its normalized bbox and the
    drawing scale. No-op when scale is unknown (leaves sizes absent so the 3D
    side can prompt for calibration instead of trusting a hallucinated value).
    """
    if not scale_m_per_px or scale_m_per_px <= 0 or not img_w or not img_h:
        return instances
    for inst in instances:
        bbox = inst.get(bbox_key)
        if not bbox or len(bbox) < 4:
            continue
        if not inst.get("width_m"):
            inst["width_m"] = round(bbox[2] * img_w * scale_m_per_px, 3)
        if not inst.get("height_m"):
            inst["height_m"] = round(bbox[3] * img_h * scale_m_per_px, 3)
    return instances


def assign_group(regions, group):
    """Tag each region with a group key (for coloring + stringify sectioning)."""
    for reg in regions:
        reg["group"] = group
    return regions


# ---------------------------------------------------------------------------
# Handwriting OCR (opt-in; panel-only, no coordinates)
# ---------------------------------------------------------------------------
ANNOTATION_KINDS = {"dimension", "area", "note", "tag", "scale", "other"}

HANDWRITING_DIRECTIVE = (
    "HANDWRITING MODE ON: transcribe ONLY handwritten, margin, or red-ink "
    "annotations that are NOT part of the printed drawing typeface (e.g. red pen "
    "mm dimensions along walls, survey corrections, scribbled notes, circled "
    "values). Do NOT repeat printed room names, title-block text, or dimensions "
    "already typeset on the plan. Put each note in the `handwriting` array as a "
    "verbatim string; set `kind` (dimension/area/note/tag/scale/other); parse a "
    "metric length into `value_mm` when obvious (e.g. 4370 -> 4370); do NOT "
    "output coordinates."
)


def handwriting_schema_item():
    """Claude structured-output item for one handwritten note."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "kind": {"type": "string"},
            "value_mm": {"type": "number"},
            "confidence": {"type": "number"},
        },
    }


def handwriting_extra_text(enabled=False):
    """Gated prompt suffix; empty when handwriting mode is off."""
    return HANDWRITING_DIRECTIVE if enabled else ""


def _parse_value_mm_from_text(text):
    """Best-effort mm parse from a dimension note like '4370' or '3110 mm'."""
    import re

    raw = str(text).replace(",", "").replace(" ", "")
    m = re.search(r"(\d{3,5})", raw)
    if m:
        return float(m.group(1))
    return None


def normalize_handwriting_items(items):
    """Map Claude handwriting[] -> strict annotations[] entries."""
    out = []
    for item in items or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        kind = str(item.get("kind") or "other").strip().lower().replace(" ", "_")
        if kind not in ANNOTATION_KINDS:
            kind = "other"
        ann = {"text": text, "kind": kind}
        value_mm = item.get("value_mm")
        if value_mm is None and kind == "dimension":
            value_mm = _parse_value_mm_from_text(text)
        if value_mm is not None:
            try:
                v = float(value_mm)
                if v > 0:
                    ann["value_mm"] = round(v, 1)
            except (TypeError, ValueError):
                pass
        conf = item.get("confidence")
        if conf is not None:
            ann["confidence"] = float(conf)
        out.append(ann)
    return out


def attach_handwriting_annotations(graph, label_data, enabled=False):
    """Copy normalized handwriting notes into graph.annotations[] when enabled."""
    if not enabled:
        return graph
    items = []
    if isinstance(label_data, dict):
        items = label_data.get("handwriting") or []
    elif isinstance(label_data, list):
        items = label_data
    anns = normalize_handwriting_items(items)
    if anns:
        graph["annotations"] = anns
        graph.setdefault("meta", {}).setdefault("warnings", []).append(
            f"handwriting_captured: {len(anns)} notes"
        )
    return graph
