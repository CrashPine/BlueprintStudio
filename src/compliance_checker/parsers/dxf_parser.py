"""
parsers/dxf_parser.py
=====================
Read an AutoCAD DXF floor plan and emit the standardized Geometry Model
(`engine.rules.GeometryObject`).

What it does:
  1. Opens the DXF with `ezdxf`.
  2. On the ROOMS layer: every closed LWPOLYLINE becomes a Shapely Polygon
     -> a Room GeometryObject. Its `type` (ER / MDA / ...) is taken from any
     TEXT/MTEXT label whose insertion point falls inside the polygon.
  3. On the RACKS layer: every closed LWPOLYLINE becomes a Rack GeometryObject.
  4. Aisles are *derived*: racks are grouped into parallel rows and the empty
     band between two adjacent rows becomes an Aisle GeometryObject, whose
     `width` metric is the gap distance. Cold/Hot is alternated as a reasonable
     PoC heuristic (front-to-front = Cold).

Computed metrics:
  Room  -> {'area', 'width'}        (width = min side of bounding box)
  Rack  -> {'area', 'width', 'depth'}
  Aisle -> {'width', 'length', 'area', 'clearance'}
"""

from __future__ import annotations

import os

import ezdxf
from shapely.geometry import Polygon

from src.compliance_checker.engine.rules import GeometryObject, TargetClass

# Layer names we look for. Adjust to match your CAD conventions.
LAYER_ROOMS = "ROOMS"
LAYER_RACKS = "RACKS"
LAYER_TEXT = "TEXT"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _polyline_to_polygon(entity) -> Polygon | None:
    """Convert a (closed) LWPOLYLINE entity into a Shapely Polygon."""
    points = [(p[0], p[1]) for p in entity.get_points("xy")]
    if len(points) < 3:
        return None
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)  # repair self-touching rings
    return poly if poly.area > 0 else None


def _bbox_metrics(poly: Polygon) -> dict[str, float]:
    """Return area + bounding-box width/depth (width = shorter side)."""
    minx, miny, maxx, maxy = poly.bounds
    dx, dy = maxx - minx, maxy - miny
    return {
        "area": round(poly.area, 4),
        "width": round(min(dx, dy), 4),
        "depth": round(max(dx, dy), 4),
    }


def _label_for_polygon(poly: Polygon, labels: list[tuple[str, float, float]]) -> str | None:
    """Find a text label whose insertion point lies inside the polygon."""
    from shapely.geometry import Point
    for text, x, y in labels:
        if poly.contains(Point(x, y)):
            return _normalise_type(text)
    return None


def _normalise_type(text: str) -> str:
    """Map a free-text label to a canonical type token."""
    t = text.strip().upper()
    known = {
        "ER": "ER", "ENTRANCE ROOM": "ER",
        "MDA": "MDA", "MAIN DISTRIBUTION AREA": "MDA",
        "HDA": "HDA", "EDA": "EDA",
        "COLD": "Cold", "HOT": "Hot",
    }
    return known.get(t, text.strip())


# ---------------------------------------------------------------------------
# Aisle derivation
# ---------------------------------------------------------------------------
def _derive_aisles(racks: list[GeometryObject]) -> list[GeometryObject]:
    """
    Find aisles as the empty bands between adjacent parallel rows of racks.

    PoC heuristic:
      * Group racks into rows by their bounding-box center Y (racks whose
        centers are within half a rack-depth of each other share a row).
      * Sort rows by Y; the vertical gap between row i and row i+1 is an aisle.
      * Aisle width  = gap between the rows (inner edge to inner edge).
        Aisle length = horizontal overlap of the two rows.
    """
    if len(racks) < 2:
        return []

    # Bounding boxes: (rack, minx, miny, maxx, maxy, cy)
    boxes = []
    for r in racks:
        minx, miny, maxx, maxy = r.geometry.bounds
        boxes.append((r, minx, miny, maxx, maxy, (miny + maxy) / 2.0))

    # Typical rack depth -> grouping tolerance.
    depths = [(maxy - miny) for _, _, miny, _, maxy, _ in boxes]
    tol = (sum(depths) / len(depths)) * 0.6

    # Group into rows by center-Y.
    boxes.sort(key=lambda b: b[5])
    rows: list[list] = []
    for b in boxes:
        if rows and abs(b[5] - rows[-1][-1][5]) <= tol:
            rows[-1].append(b)
        else:
            rows.append([b])

    aisles: list[GeometryObject] = []
    for i in range(len(rows) - 1):
        lower, upper = rows[i], rows[i + 1]

        # Inner edges of the band between the two rows.
        lower_top = max(b[4] for b in lower)      # highest maxy in lower row
        upper_bottom = min(b[2] for b in upper)   # lowest miny in upper row
        gap = upper_bottom - lower_top
        if gap <= 0:
            continue  # rows overlap -> not a clean aisle

        # Horizontal extent = overlap of the two rows' X ranges.
        x0 = max(min(b[1] for b in lower), min(b[1] for b in upper))
        x1 = min(max(b[3] for b in lower), max(b[3] for b in upper))
        if x1 <= x0:
            continue
        length = x1 - x0

        band = Polygon([(x0, lower_top), (x1, lower_top),
                        (x1, upper_bottom), (x0, upper_bottom)])

        # Alternate Cold/Hot: convention here = first gap is a Cold aisle.
        aisle_type = "Cold" if i % 2 == 0 else "Hot"
        aisles.append(GeometryObject(
            id=f"aisle-{i + 1:02d}",
            **{"class": TargetClass.AISLE},
            type=aisle_type,
            geometry=band,
            calculated_metrics={
                "width": round(gap, 4),
                "length": round(length, 4),
                "area": round(band.area, 4),
                "clearance": round(gap, 4),
            },
        ))
    return aisles


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def parse_dxf(path: str) -> list[GeometryObject]:
    """
    Parse a DXF file into a list of standardized GeometryObjects
    (Rooms, Racks and derived Aisles).
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"DXF file not found: {path}")

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()

    # 1. Collect text labels (text, x, y) for type assignment.
    labels: list[tuple[str, float, float]] = []
    for e in msp.query("TEXT MTEXT"):
        try:
            txt = e.dxf.text if e.dxftype() == "TEXT" else e.text
            ins = e.dxf.insert
            labels.append((str(txt), float(ins[0]), float(ins[1])))
        except Exception:  # noqa: BLE001 - skip malformed text entities
            continue

    objects: list[GeometryObject] = []

    # 2. Rooms.
    for idx, e in enumerate(msp.query(f'LWPOLYLINE[layer=="{LAYER_ROOMS}"]'), start=1):
        if not e.closed:
            continue
        poly = _polyline_to_polygon(e)
        if poly is None:
            continue
        metrics = _bbox_metrics(poly)
        room_type = _label_for_polygon(poly, labels)
        objects.append(GeometryObject(
            id=f"room-{idx:02d}",
            **{"class": TargetClass.ROOM},
            type=room_type,
            geometry=poly,
            calculated_metrics={"area": metrics["area"], "width": metrics["width"]},
        ))

    # 3. Racks.
    racks: list[GeometryObject] = []
    for idx, e in enumerate(msp.query(f'LWPOLYLINE[layer=="{LAYER_RACKS}"]'), start=1):
        if not e.closed:
            continue
        poly = _polyline_to_polygon(e)
        if poly is None:
            continue
        metrics = _bbox_metrics(poly)
        rack = GeometryObject(
            id=f"rack-{idx:02d}",
            **{"class": TargetClass.RACK},
            type=_label_for_polygon(poly, labels),
            geometry=poly,
            calculated_metrics=metrics,
        )
        racks.append(rack)
    objects.extend(racks)

    # 4. Derived aisles.
    objects.extend(_derive_aisles(racks))

    print(f"[dxf_parser] Parsed {len(objects)} objects "
          f"({sum(1 for o in objects if o.cls == TargetClass.ROOM.value)} rooms, "
          f"{len(racks)} racks, "
          f"{sum(1 for o in objects if o.cls == TargetClass.AISLE.value)} aisles).")
    return objects
