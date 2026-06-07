"""
Overlay a parsed graph JSON back onto its source image.

Draws node bounding boxes + tags, edges (colored by medium), and room polygons
with names / areas / dimensions. Works for any graph produced by the
Claude / YOLO / floor-plan / datacenter parsers (coords normalized 0-1).

Key properties:
  * Labels never overlap each other, never cover equipment symbols, and stay on
    the page; when a label is nudged away from its anchor a thin leader line is
    drawn so the association stays clear.
  * Labels are placed by priority (big rooms, then equipment, then small
    instruments) so the most important text wins the good positions.
  * Areas/dimensions render in the drawing's own unit system, preferring the
    verbatim annotation when present.
  * A scale bar is drawn whenever meta.scale_m_per_px is known.
  * --max-labels caps label count on very dense P&IDs (symbols still drawn).

CLI:
    python scripts/visualize.py data/parsed_claude.json data/raw/plan.jpg
    python scripts/visualize.py <graph.json> <image> --out overlay.png --max-labels 120
"""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

M2_PER_SQFT = 0.092903

# ---------------------------------------------------------------------------
# Text rendering — Unicode via Pillow (room names, m², é…), ASCII Hershey fallback
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except Exception:  # pragma: no cover - Pillow always present in this env
    _PIL_OK = False

_FONT_CANDIDATES = {
    False: [
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
    True: [
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
}
_font_cache = {}


def _font(px, bold=False):
    if not _PIL_OK:
        return None
    key = (px, bold)
    if key not in _font_cache:
        loaded = None
        for path in _FONT_CANDIDATES[bold]:
            try:
                loaded = ImageFont.truetype(path, px)
                break
            except Exception:
                continue
        _font_cache[key] = loaded or ImageFont.load_default()
    return _font_cache[key]


def _font_px(font_scale):
    """Map a cv2-style font_scale to a visually comparable Pillow pixel size."""
    return max(11, int(round(font_scale * 34)))


# transliteration so the Hershey fallback never prints "??"
_ASCII_MAP = str.maketrans({
    "\u00b2": "2", "\u00b3": "3", "\u00d7": "x", "\u00e9": "e", "\u00e8": "e",
    "\u00ea": "e", "\u00eb": "e", "\u00e0": "a", "\u00e2": "a", "\u00e4": "a",
    "\u00e7": "c", "\u00f4": "o", "\u00f6": "o", "\u00fb": "u", "\u00fc": "u",
    "\u00ee": "i", "\u00ef": "i", "\u00f9": "u", "\u00df": "ss",
    "\u2013": "-", "\u2014": "-", "\u00b0": "deg",
})


def _ascii(s):
    return str(s).translate(_ASCII_MAP)


def _measure_lines(lines, font_px, line_gap):
    """Return (max_width, total_height, line_height) for a block of text lines."""
    if _PIL_OK:
        f = _font(font_px)
        asc, desc = f.getmetrics()
        line_h = asc + desc
        width = max((int(f.getbbox(t)[2]) for t in lines), default=0)
    else:
        fs = font_px / 34.0
        sizes = [cv2.getTextSize(_ascii(t), cv2.FONT_HERSHEY_SIMPLEX, fs, 1) for t in lines]
        width = max((tw for (tw, _), _ in sizes), default=0)
        line_h = max((th + b for (_, th), b in sizes), default=0)
    total_h = line_h * len(lines) + line_gap * max(len(lines) - 1, 0)
    return width, total_h, line_h


# ---------------------------------------------------------------------------
# Color tables (BGR)
# ---------------------------------------------------------------------------
TYPE_COLORS = {
    # P&ID high-level
    "instrument": (0, 200, 200), "sensor": (0, 200, 200), "transmitter": (0, 200, 200),
    "controller": (0, 200, 200), "indicator": (0, 200, 200), "signal": (200, 0, 100),
    "valve": (0, 140, 200), "fitting": (150, 150, 150), "reducer": (150, 150, 150),
    "strainer": (150, 150, 150), "trap": (150, 150, 150), "flange": (150, 150, 150),
    "equipment": (200, 120, 0), "tank": (200, 120, 0), "vessel": (200, 120, 0),
    "column": (200, 120, 0), "tower": (80, 160, 0), "drum": (200, 120, 0),
    "reactor": (200, 120, 0), "separator": (200, 120, 0),
    "heat_exchanger": (200, 120, 0), "condenser": (200, 120, 0), "evaporator": (200, 120, 0),
    # HVAC
    "chiller": (200, 120, 0), "pump": (200, 120, 0), "cooling_tower": (80, 160, 0),
    "boiler": (0, 80, 200), "compressor": (200, 120, 0),
    "ahu": (0, 160, 0), "fcu": (0, 160, 0), "fan": (0, 160, 0), "crac": (0, 160, 0),
    "crah": (0, 160, 0), "vav": (0, 160, 0), "damper": (0, 160, 0), "filter": (0, 160, 0),
    "humidifier": (0, 160, 0), "economizer": (0, 160, 0), "coil": (0, 160, 0),
    "diffuser": (0, 160, 0),
    # Electrical
    "transformer": (40, 40, 220), "switchgear": (40, 40, 220), "breaker": (40, 40, 220),
    "distribution_panel": (40, 40, 220), "panelboard": (40, 40, 220), "meter": (40, 40, 220),
    "busway": (40, 40, 220), "generator": (40, 40, 220), "ats": (40, 40, 220),
    "mcc": (40, 40, 220), "vfd": (40, 40, 220), "capacitor_bank": (40, 40, 220),
    "disconnect": (40, 40, 220), "relay": (40, 40, 220), "fuse": (40, 40, 220),
    "rectifier": (40, 40, 220), "inverter": (40, 40, 220), "motor": (40, 40, 220),
    "ups": (160, 0, 160), "pdu": (160, 0, 160), "battery": (160, 0, 160),
    "rack": (120, 0, 120),
    "unknown": (130, 130, 130),
}
DEFAULT_COLOR = (130, 130, 130)
EDGE_DEFAULT_COLOR = (90, 90, 90)

# Edge color by medium (BGR)
EDGE_MEDIUM_COLORS = {
    "water_chilled": (200, 120, 0), "water_condenser": (160, 160, 0),
    "water_hot": (40, 40, 220), "water_cold_domestic": (200, 160, 0),
    "steam": (120, 120, 200), "condensate": (150, 150, 200),
    "refrigerant": (180, 0, 180), "glycol": (160, 120, 0),
    "air_supply": (0, 160, 0), "air_return": (0, 120, 120), "air_exhaust": (60, 60, 60),
    "electrical_power": (40, 40, 220), "electrical_control": (0, 140, 200),
    "data": (160, 0, 160), "signal": (200, 0, 100),
    "unknown": (90, 90, 90),
}

CATEGORY_COLORS = {
    # data centre / MEP
    "data_hall": (0, 180, 255), "white_space": (0, 180, 255), "server_room": (0, 200, 220),
    "network_room": (0, 200, 220), "noc": (0, 200, 220), "command_center": (0, 200, 220),
    "electrical_room": (40, 40, 220), "switchgear_room": (40, 40, 220),
    "transformer_room": (40, 40, 220), "generator_room": (40, 40, 220),
    "ups_room": (160, 0, 160), "battery_room": (160, 0, 160),
    "plant_room": (200, 120, 0), "mechanical_room": (180, 120, 40),
    "chiller_plant": (200, 120, 0), "boiler_room": (0, 80, 200), "pump_room": (200, 120, 0),
    "hot_aisle": (60, 90, 200), "cold_aisle": (200, 140, 60), "raised_floor": (150, 160, 170),
    # living
    "bedroom": (200, 150, 80), "master_bedroom": (200, 140, 70), "guest_bedroom": (200, 160, 90),
    "nursery": (200, 170, 110),
    "living_room": (90, 180, 90), "family_room": (90, 180, 100), "great_room": (90, 190, 90),
    "sitting_room": (110, 180, 110), "den": (110, 170, 110),
    "dining_room": (60, 160, 120), "breakfast_nook": (80, 170, 130),
    "kitchen": (40, 170, 200), "kitchenette": (60, 180, 200), "pantry": (120, 180, 200),
    "bathroom": (200, 180, 80), "ensuite": (200, 180, 100), "powder_room": (200, 190, 110),
    "toilet": (200, 200, 120), "wc": (200, 200, 120), "shower_room": (200, 190, 90),
    "sauna": (90, 130, 210), "sauna_room": (110, 150, 210), "dressing_room": (180, 160, 120),
    "laundry": (160, 160, 90), "mudroom": (150, 150, 100), "utility": (130, 150, 150),
    "closet": (140, 150, 160), "walk_in_closet": (150, 150, 160), "wardrobe": (150, 150, 160),
    "linen_closet": (150, 155, 165),
    # circulation
    "corridor": (150, 150, 150), "hallway": (150, 150, 150), "foyer": (170, 170, 130),
    "entry": (170, 170, 130), "vestibule": (170, 170, 135), "landing": (160, 160, 140),
    "lobby": (170, 170, 130), "atrium": (170, 175, 140), "reception": (170, 150, 130),
    "waiting_area": (170, 160, 140),
    "stairwell": (110, 110, 170), "staircase": (110, 110, 170), "elevator": (110, 110, 170),
    "escalator": (110, 110, 170),
    # work / commercial
    "office": (120, 160, 200), "home_office": (120, 160, 200), "open_office": (120, 165, 205),
    "private_office": (120, 155, 195), "cubicle_area": (130, 165, 200),
    "meeting_room": (120, 140, 200), "conference_room": (120, 140, 200),
    "boardroom": (120, 135, 200), "phone_booth": (130, 150, 200),
    "break_room": (140, 170, 180), "cafeteria": (120, 175, 180), "canteen": (120, 175, 180),
    "classroom": (120, 180, 160), "lecture_hall": (120, 180, 160),
    "laboratory": (140, 180, 170), "cleanroom": (150, 190, 180), "retail": (160, 120, 200),
    "showroom": (160, 130, 200), "warehouse": (130, 140, 150),
    # storage / aux / outdoor
    "garage": (110, 110, 110), "carport": (120, 120, 120),
    "storage": (130, 140, 150), "stockroom": (130, 140, 150), "spares": (130, 140, 150),
    "loading_dock": (120, 130, 140), "shipping": (120, 130, 140),
    "balcony": (90, 190, 150), "terrace": (90, 200, 160), "patio": (90, 200, 170),
    "deck": (90, 200, 165), "porch": (95, 195, 160), "veranda": (95, 195, 165),
    "loggia": (95, 195, 160), "garden": (60, 200, 120), "yard": (70, 200, 130),
    "courtyard": (80, 200, 140),
    "basement": (120, 120, 130), "attic": (130, 130, 140), "cellar": (110, 110, 120),
    "wine_cellar": (60, 60, 150),
    "shaft": (120, 120, 130), "riser": (120, 120, 130), "duct_shaft": (120, 120, 130),
    "mechanical_shaft": (120, 120, 130), "plenum": (130, 130, 140),
    "security": (90, 90, 160), "mantrap": (90, 90, 160), "control_room": (100, 100, 170),
    "parking": (110, 110, 110), "driveway": (120, 120, 120), "roof": (140, 140, 150),
    "rooftop": (140, 140, 150),
    "room": (160, 160, 160), "space": (160, 160, 160), "zone": (160, 160, 160),
    "area": (160, 160, 160), "unknown": (160, 160, 160),
}
SPACE_DEFAULT_COLOR = (0, 180, 255)

# Fixtures (doors/windows/sanitary/appliances) — BGR
FIXTURE_COLORS = {
    "door": (255, 80, 0), "sliding_door": (255, 100, 0), "double_door": (255, 60, 0),
    "folding_door": (255, 120, 0), "garage_door": (255, 140, 0),
    "window": (255, 200, 0), "opening": (200, 200, 0),
    "toilet": (200, 0, 200), "sink": (200, 60, 200), "bathtub": (180, 0, 180),
    "shower": (160, 0, 200), "bidet": (200, 40, 180),
    "stove": (0, 100, 220), "refrigerator": (80, 120, 220), "dishwasher": (60, 140, 220),
    "washer": (100, 100, 220), "dryer": (120, 120, 220),
    "stairs": (110, 110, 170), "elevator_car": (110, 110, 170), "column": (90, 90, 90),
    "unknown": (150, 150, 150),
}
FIXTURE_DEFAULT_COLOR = (200, 0, 200)


def _category_color(category):
    return CATEGORY_COLORS.get(category, SPACE_DEFAULT_COLOR)


def _fixture_color(category):
    return FIXTURE_COLORS.get(category, FIXTURE_DEFAULT_COLOR)


def _type_color(node_type):
    return TYPE_COLORS.get(node_type, DEFAULT_COLOR)


def _edge_color(edge):
    return EDGE_MEDIUM_COLORS.get(edge.get("medium"), EDGE_DEFAULT_COLOR)


# ---------------------------------------------------------------------------
# Label placement
# ---------------------------------------------------------------------------
class LabelPlacer:
    """
    Places multi-line text labels with opaque backgrounds, avoiding:
      * other labels already placed,
      * registered obstacles (equipment symbols, legend),
      * the page edges.
    Draws a leader line when a label is pushed far from its anchor.
    """

    def __init__(self, width, height, render_scale=1.0):
        self.width = width
        self.height = height
        self.rs = float(render_scale)
        self.placed = []      # label boxes already drawn
        self.obstacles = []   # symbol/legend boxes to avoid covering
        self.text_jobs = []   # deferred text, composited in one Pillow pass

    # --- geometry helpers ---
    @staticmethod
    def _overlaps(a, b):
        return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

    def add_obstacle(self, box):
        self.obstacles.append(tuple(int(v) for v in box))

    def _collides(self, box):
        if box[0] < 2 or box[1] < 2 or box[2] > self.width - 2 or box[3] > self.height - 2:
            return True
        for other in self.placed:
            if self._overlaps(box, other):
                return True
        for other in self.obstacles:
            if self._overlaps(box, other):
                return True
        return False

    def _measure(self, lines, font_px, pad, line_gap):
        width, total_h, line_h = _measure_lines(lines, font_px, line_gap)
        return width + 2 * pad, total_h + 2 * pad, line_h

    def place(self, img, lines, anchor, color, font_scale=0.45, thickness=1,
              pad=3, line_gap=3, draw_leader=True, leader_threshold=14):
        """Draw `lines` (str or list[str]) near anchor (x, y); return chosen box."""
        if isinstance(lines, str):
            lines = [lines]
        lines = [l for l in lines if l]
        if not lines:
            return None

        font_px = _font_px(font_scale * self.rs)
        pad = max(2, int(round(pad * self.rs)))
        line_gap = max(1, int(round(line_gap * self.rs)))
        border = max(1, int(round(self.rs)))
        bw, bh, line_h = self._measure(lines, font_px, pad, line_gap)
        ax, ay = int(anchor[0]), int(anchor[1])

        # candidate top-left positions: centred on the anchor, then spiral out
        candidates = [(ax - bw // 2, ay - bh // 2)]
        step = max(bh // 2, 10)
        for ring in range(1, 22):
            d = ring * step
            base_x = ax - bw // 2
            base_y = ay - bh // 2
            candidates += [
                (base_x, base_y - d), (base_x, base_y + d),
                (base_x + d, base_y), (base_x - d, base_y),
                (base_x + d, base_y - d), (base_x - d, base_y - d),
                (base_x + d, base_y + d), (base_x - d, base_y + d),
            ]

        chosen = None
        for x1, y1 in candidates:
            box = (x1, y1, x1 + bw, y1 + bh)
            if not self._collides(box):
                chosen = box
                break
        if chosen is None:  # give up gracefully — clamp into the page
            x1 = min(max(ax - bw // 2, 2), self.width - bw - 2)
            y1 = min(max(ay - bh // 2, 2), self.height - bh - 2)
            chosen = (x1, y1, x1 + bw, y1 + bh)

        x1, y1, x2, y2 = chosen

        # leader line from anchor to the label if it was displaced
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        if draw_leader and (abs(cx - ax) + abs(cy - ay)) > (bh + leader_threshold):
            edge_pt = (int(np.clip(ax, x1, x2)), int(np.clip(ay, y1, y2)))
            cv2.line(img, (ax, ay), edge_pt, color, border, cv2.LINE_AA)
            cv2.circle(img, (ax, ay), 2 * border, color, -1, cv2.LINE_AA)

        # background + border (cv2); text deferred to the Pillow pass
        cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 255), -1)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, border)

        ty = y1 + pad
        for line in lines:
            self.queue_text(line, (x1 + pad, ty), color, font_px)
            ty += line_h + line_gap

        self.placed.append(chosen)
        return chosen

    # --- deferred text (rendered together in one Pillow pass) ---
    def queue_text(self, text, top_left, color_bgr, font_px, bold=False):
        self.text_jobs.append((
            str(text), (int(top_left[0]), int(top_left[1])),
            tuple(int(c) for c in color_bgr), int(font_px), bool(bold),
        ))

    def render_text(self, img):
        """Composite all queued text onto img (BGR) and return the result."""
        if not self.text_jobs:
            return img
        if not _PIL_OK:
            for text, (x, y), color, font_px, _bold in self.text_jobs:
                fs = font_px / 34.0
                (_, th), _ = cv2.getTextSize(_ascii(text), cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
                cv2.putText(img, _ascii(text), (x, y + th),
                            cv2.FONT_HERSHEY_SIMPLEX, fs, color, 1, cv2.LINE_AA)
            return img
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil)
        for text, (x, y), color, font_px, bold in self.text_jobs:
            draw.text((x, y), text, font=_font(font_px, bold),
                      fill=(color[2], color[1], color[0]))
        return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


# ---------------------------------------------------------------------------
# Coordinate + label helpers
# ---------------------------------------------------------------------------
def _bbox_to_px(bbox, w, h):
    cx, cy, bw, bh = bbox
    return (int((cx - bw / 2) * w), int((cy - bh / 2) * h),
            int((cx + bw / 2) * w), int((cy + bh / 2) * h))


def _node_center_px(node, w, h):
    bbox = node.get("bbox_2d")
    if not bbox:
        return None
    return int(bbox[0] * w), int(bbox[1] * h)


def _fmt_num(v):
    """Trim trailing zeros: 11.20 -> '11.2', 14.0 -> '14'."""
    return f"{float(v):g}"


def _space_label_lines(sp, unit_system):
    """Return up to three lines: name(/number), area, dimensions."""
    lines = []
    name = sp.get("name") or sp.get("id") or "room"
    num = sp.get("room_number")
    lines.append(f"{name} ({num})" if num else f"{name}")

    # area: prefer verbatim annotation, else format the computed value
    area_raw = sp.get("area_raw")
    area_m2 = sp.get("area_m2")
    if area_raw:
        lines.append(area_raw.replace("m2", "m\u00b2"))
    elif area_m2 not in (None, "?", 0):
        if unit_system == "imperial":
            lines.append(f"{area_m2 / M2_PER_SQFT:.0f} sq ft")
        else:
            lines.append(f"{_fmt_num(area_m2)} m\u00b2")

    # dimensions
    dims_raw = sp.get("dimensions_raw")
    wm, hm = sp.get("width_m"), sp.get("height_m")
    if dims_raw:
        lines.append(dims_raw)
    elif wm and hm and unit_system != "imperial":
        lines.append(f"{_fmt_num(wm)}\u00d7{_fmt_num(hm)} m")
    return lines


def _node_label_lines(node):
    cls = node.get("type", "?")
    sub = node.get("sub_type")
    cls = f"{cls}.{sub}" if sub else cls
    tag = node.get("tag")
    return [cls, f"[{tag}]"] if tag else [cls]


def _node_priority(node):
    """Bigger / tagged equipment is more important to label."""
    bbox = node.get("bbox_2d") or [0, 0, 0, 0]
    area = float(bbox[2]) * float(bbox[3])
    big = node.get("type") in {
        "chiller", "pump", "cooling_tower", "boiler", "tank", "vessel", "tower",
        "transformer", "switchgear", "generator", "ups", "ahu", "distribution_panel",
        "mcc", "rack", "pdu",
    }
    return (1 if big else 0, 1 if node.get("tag") else 0, area)


def _nice_scale_length(target_m):
    """Round target metres to a clean bar length (1,2,5 x 10^k)."""
    if target_m <= 0:
        return 1
    import math
    exp = math.floor(math.log10(target_m))
    base = 10 ** exp
    for m in (1, 2, 5, 10):
        if m * base >= target_m:
            return m * base
    return 10 * base


def _render_scale(w, h):
    """Scale stroke widths / label text with image resolution (1.0–2.6x)."""
    return float(np.clip(max(w, h) / 1400.0, 1.0, 2.6))


def _load_image(path):
    """Read an image; fall back to Pillow so .webp datacenter plans still load."""
    img = cv2.imread(str(path))
    if img is not None:
        return img
    try:
        from PIL import Image as _PILImage

        pil = _PILImage.open(path).convert("RGB")
        arr = np.array(pil)
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    except Exception as exc:
        raise FileNotFoundError(f"Cannot read image: {path}") from exc


def _graph_layers(graph):
    """Normalize datacenter graph keys (zones/equipment) to spaces/nodes."""
    spaces = graph.get("spaces") or graph.get("zones") or []
    nodes = graph.get("nodes") or []
    if not nodes and graph.get("equipment"):
        for eq in graph["equipment"]:
            nodes.append({
                "id": eq["id"],
                "type": eq.get("type", "unknown"),
                "sub_type": eq.get("sub_type"),
                "tag": eq.get("tag", eq["id"]),
                "bbox_2d": eq.get("bbox_2d"),
                "confidence": eq.get("confidence", 0.8),
            })
    return spaces, nodes


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------
def overlay_graph(image_path, graph, out_path=None, draw_edges=True, draw_labels=True,
                  max_labels=None, label_obstacles=True, show_scale_bar=True,
                  draw_annotations=True):
    """
    Render a parsed graph over its source image. Returns the output path.
    Pure function — usable from notebooks, the API, or the CLI.

    max_labels:      cap on the number of node labels (LOD for dense P&IDs).
    label_obstacles: labels avoid covering equipment symbols when True.
    show_scale_bar:  draw a metric scale bar when meta.scale_m_per_px is known.
    """
    img = _load_image(image_path)
    h, w = img.shape[:2]
    overlay = img.copy()
    rs = _render_scale(w, h)
    stroke = max(1, int(round(2 * rs)))
    placer = LabelPlacer(w, h, render_scale=rs)
    meta = graph.get("meta") or {}
    unit_system = meta.get("unit_system")
    spaces, nodes = _graph_layers(graph)

    # 1. Room polygons — translucent fill + solid outline colored by category
    room_labels = []
    for sp in spaces:
        poly = sp.get("polygon_2d") or []
        if len(poly) < 3:
            continue
        pts = np.array([[int(x * w), int(y * h)] for x, y in poly], dtype=np.int32)
        color = _category_color(sp.get("category"))
        cv2.fillPoly(overlay, [pts], color)
        cv2.polylines(img, [pts], True, color, stroke)
        cx = int(pts[:, 0].mean())
        cy = int(pts[:, 1].mean())
        room_labels.append((sp, color, (cx, cy), cv2.contourArea(pts)))
    img = cv2.addWeighted(overlay, 0.22, img, 0.78, 0)

    # 2. Edges (colored by medium)
    if draw_edges:
        centers = {n["id"]: _node_center_px(n, w, h) for n in nodes}
        for edge in graph.get("edges") or []:
            a = centers.get(edge.get("from"))
            b = centers.get(edge.get("to"))
            if a and b:
                cv2.line(img, a, b, _edge_color(edge), max(1, int(round(rs))), cv2.LINE_AA)

    # 3. Node boxes (drawn for all nodes; registered as label obstacles)
    node_label_jobs = []
    for node in nodes:
        bbox = node.get("bbox_2d")
        if not bbox:
            continue
        x1, y1, x2, y2 = _bbox_to_px(bbox, w, h)
        color = _type_color(node.get("type"))
        cv2.rectangle(img, (x1, y1), (x2, y2), color, stroke)
        if label_obstacles:
            placer.add_obstacle((x1, y1, x2, y2))
        anchor = ((x1 + x2) // 2, max(y1 - 6, 10))
        node_label_jobs.append((_node_priority(node), node, color, anchor))

    # 3b. Fixtures (doors/windows/sanitary) — boxes + short category tag
    fixture_label_jobs = []
    for fx in graph.get("fixtures") or []:
        bbox = fx.get("bbox_2d")
        if not bbox:
            continue
        x1, y1, x2, y2 = _bbox_to_px(bbox, w, h)
        color = _fixture_color(fx.get("category"))
        cv2.rectangle(img, (x1, y1), (x2, y2), color, max(1, stroke - 1))
        if label_obstacles:
            placer.add_obstacle((x1, y1, x2, y2))
        anchor = ((x1 + x2) // 2, max(y1 - 4, 10))
        fixture_label_jobs.append((fx, color, anchor))

    # 4. Legend + annotations panel + scale bar first, so labels route around them
    _draw_legend(img, meta, spaces, unit_system, placer)
    if draw_annotations:
        _draw_annotations_panel(img, graph.get("annotations") or [], placer)
    if show_scale_bar and meta.get("scale_m_per_px"):
        _draw_scale_bar(img, meta["scale_m_per_px"], placer)

    if not draw_labels:
        return _write(placer.render_text(img), image_path, out_path)

    # 5a. Room labels — largest rooms first (reliable centroids win good spots)
    for sp, color, center, _area in sorted(room_labels, key=lambda r: r[3], reverse=True):
        placer.place(img, _space_label_lines(sp, unit_system), center, color, font_scale=0.45)

    # 5b. Node labels — most significant first; cap for dense diagrams
    node_label_jobs.sort(key=lambda j: j[0], reverse=True)
    if max_labels is not None:
        node_label_jobs = node_label_jobs[:max_labels]
    for _prio, node, color, anchor in node_label_jobs:
        placer.place(img, _node_label_lines(node), anchor, color, font_scale=0.4)

    # 5c. Fixture labels — short category text (doors/windows/sanitary)
    for fx, color, anchor in fixture_label_jobs:
        cat = fx.get("category", "?")
        wm, hm = fx.get("width_m"), fx.get("height_m")
        lines = [cat]
        if wm and hm:
            lines.append(f"{_fmt_num(wm)}\u00d7{_fmt_num(hm)} m")
        placer.place(img, lines, anchor, color, font_scale=0.36)

    return _write(placer.render_text(img), image_path, out_path)


def _draw_legend(img, meta, spaces, unit_system, placer):
    n_rooms = meta.get("room_count", len(spaces))
    total_area = meta.get("total_floor_area_m2") or meta.get("total_area_m2")
    if total_area is None and spaces:
        total_area = round(sum(float(s.get("area_m2") or 0) for s in spaces), 1)

    bits = []
    if meta.get("title"):
        bits.append(str(meta["title"])[:46])
    if spaces:
        line = f"Rooms: {n_rooms}"
        if total_area:
            if unit_system == "imperial":
                line += f" | Total: {total_area / M2_PER_SQFT:.0f} sq ft"
            else:
                line += f" | Total: {total_area} m\u00b2"
        bits.append(line)
    if not bits:
        return

    rs = placer.rs
    font_px = _font_px(0.6 * rs)
    pad = max(5, int(round(7 * rs)))
    line_gap = max(3, int(round(4 * rs)))
    border = max(1, int(round(rs)))
    margin = max(8, int(round(8 * rs)))
    bw, total_h, line_h = _measure_lines(bits, font_px, line_gap)
    box_w, box_h = bw + 2 * pad, total_h + 2 * pad
    x0 = y0 = margin
    cv2.rectangle(img, (x0, y0), (x0 + box_w, y0 + box_h), (255, 255, 255), -1)
    cv2.rectangle(img, (x0, y0), (x0 + box_w, y0 + box_h), (40, 40, 40), border)
    y = y0 + pad
    for text in bits:
        placer.queue_text(text, (x0 + pad, y), (20, 20, 20), font_px, bold=True)
        y += line_h + line_gap
    placer.add_obstacle((x0, y0, x0 + box_w, y0 + box_h))


def _annotation_line(ann, max_chars=42):
    """Format one annotation for the side panel."""
    text = str(ann.get("text") or "").strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "\u2026"
    kind = ann.get("kind")
    if kind and kind != "other":
        text = f"[{kind}] {text}"
    value_mm = ann.get("value_mm")
    if value_mm:
        text = f"{text} ({_fmt_num(value_mm)} mm)"
    return text


def _draw_annotations_panel(img, annotations, placer, max_lines=20):
    """Top-right panel listing handwritten OCR notes (no per-note coordinates)."""
    if not annotations:
        return
    h, w = img.shape[:2]
    rs = placer.rs
    font_px = _font_px(0.45 * rs)
    pad = max(5, int(round(7 * rs)))
    line_gap = max(2, int(round(3 * rs)))
    border = max(1, int(round(rs)))
    margin = max(8, int(round(8 * rs)))

    title = f"Handwritten notes ({len(annotations)})"
    lines = [title]
    shown = annotations[:max_lines]
    for ann in shown:
        lines.append(_annotation_line(ann))
    if len(annotations) > max_lines:
        lines.append(f"\u2026 +{len(annotations) - max_lines} more")

    bw, total_h, line_h = _measure_lines(lines, font_px, line_gap)
    box_w, box_h = bw + 2 * pad, total_h + 2 * pad
    x0 = max(margin, w - box_w - margin)
    y0 = margin
    cv2.rectangle(img, (x0, y0), (x0 + box_w, y0 + box_h), (255, 255, 255), -1)
    cv2.rectangle(img, (x0, y0), (x0 + box_w, y0 + box_h), (180, 40, 40), border)
    y = y0 + pad
    for i, text in enumerate(lines):
        color = (180, 40, 40) if i == 0 else (30, 30, 30)
        placer.queue_text(text, (x0 + pad, y), color, font_px, bold=(i == 0))
        y += line_h + line_gap
    placer.add_obstacle((x0, y0, x0 + box_w, y0 + box_h))


def _draw_scale_bar(img, scale_m_per_px, placer):
    h, w = img.shape[:2]
    rs = placer.rs
    target_m = _nice_scale_length((w * 0.15) * scale_m_per_px)
    bar_px = int(target_m / scale_m_per_px)
    if bar_px < 20 or bar_px > w * 0.6:
        return
    label = f"{_fmt_num(target_m)} m"
    font_px = _font_px(0.5 * rs)
    _, txt_h, _ = _measure_lines([label], font_px, 0)
    border = max(1, int(round(rs)))
    tick = max(4, int(round(4 * rs)))
    boxpad = max(8, int(round(8 * rs)))
    x0 = boxpad + 8
    y0 = h - boxpad - 2                 # the bar line sits here
    top = y0 - txt_h - max(8, int(round(10 * rs)))
    cv2.rectangle(img, (x0 - boxpad, top), (x0 + bar_px + boxpad, y0 + tick + 4),
                  (255, 255, 255), -1)
    cv2.rectangle(img, (x0 - boxpad, top), (x0 + bar_px + boxpad, y0 + tick + 4),
                  (40, 40, 40), border)
    cv2.line(img, (x0, y0), (x0 + bar_px, y0), (20, 20, 20), border + 1)
    cv2.line(img, (x0, y0 - tick), (x0, y0 + tick), (20, 20, 20), border + 1)
    cv2.line(img, (x0 + bar_px, y0 - tick), (x0 + bar_px, y0 + tick), (20, 20, 20), border + 1)
    placer.queue_text(label, (x0, top + max(2, int(round(2 * rs)))), (20, 20, 20), font_px)
    placer.add_obstacle((x0 - boxpad, top, x0 + bar_px + boxpad, y0 + tick + 4))


def _write(img, image_path, out_path):
    if out_path is None:
        out_path = Path(image_path).with_name(Path(image_path).stem + "_overlay.png")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), img)
    return str(out_path)


def main():
    ap = argparse.ArgumentParser(description="Overlay a parsed graph onto its source image")
    ap.add_argument("graph_json", help="parsed graph JSON path")
    ap.add_argument("image", help="source image path")
    ap.add_argument("--out", default=None, help="output PNG path")
    ap.add_argument("--max-labels", type=int, default=None,
                    help="cap node labels (LOD for dense P&IDs)")
    ap.add_argument("--no-edges", action="store_true")
    ap.add_argument("--no-labels", action="store_true")
    ap.add_argument("--no-scale-bar", action="store_true")
    ap.add_argument("--no-annotations", action="store_true")
    args = ap.parse_args()

    with open(args.graph_json, "r", encoding="utf-8") as f:
        graph = json.load(f)

    out = overlay_graph(
        args.image, graph, out_path=args.out,
        draw_edges=not args.no_edges, draw_labels=not args.no_labels,
        max_labels=args.max_labels, show_scale_bar=not args.no_scale_bar,
        draw_annotations=not args.no_annotations,
    )
    spaces, nodes = _graph_layers(graph)
    n_fix = len(graph.get("fixtures") or [])
    n_ann = len(graph.get("annotations") or [])
    print(
        f"Wrote {out} | {len(nodes)} nodes, {len(spaces)} spaces, "
        f"{n_fix} fixtures, {n_ann} annotations drawn"
    )


if __name__ == "__main__":
    main()
