"""
parsers/graph_parser.py
=======================
Read an "ArchDraft x FlowDraft Unified Graph" JSON document and emit the
standardized Geometry Model (`engine.rules.GeometryObject`) consumed by the
validator. This is the primary geometry source (it replaces the DXF parser).

The graph (see contracts/graph.schema.v2.json) contains:
  * meta    - diagram + power info (pue, it_power_kW, facility_power_kW, ...)
  * spaces  - architectural rooms with `polygon_2d` (NORMALIZED 0-1) + `area_m2`
  * nodes   - equipment (chiller/crac/ups/pdu/rack/...) with `bbox_2d` (0-1)
  * edges   - MEP/electrical connections between nodes (from/to)

Key challenges & how we solve them:
  1. Coordinates are normalized 0-1 relative to the source image, so raw
     lengths are meaningless. We CALIBRATE a metres-per-normalized-unit scale
     from the spaces whose real `area_m2` is known (see `_calibrate_scale`).
     Room areas use the authoritative `area_m2` field directly; widths,
     clearances and aisle gaps are derived from the calibrated geometry.
  2. Connectivity is taken from the real graph `edges` (plus node->space
     membership), not from polygon adjacency, and stored in each object's
     `links` for the validator to assemble into a networkx graph.
"""

from __future__ import annotations

import json
import math
import os

from shapely.affinity import scale as shapely_scale
from shapely.geometry import Polygon, box

from src.compliance_checker.engine.rules import GeometryObject, TargetClass
# Reuse the aisle-derivation logic already written for the DXF parser.
from src.compliance_checker.parsers.dxf_parser import _derive_aisles


# ---------------------------------------------------------------------------
# Scale calibration
# ---------------------------------------------------------------------------
def _calibrate_scale(spaces: list[dict], meta: dict) -> tuple[float, float]:
    """
    Derive (Sx, Sy) = metres per normalized unit on each axis.

    Physics: the source image is normalized so x is divided by image_width and
    y by image_height. With a single metres-per-pixel resolution, the physical
    aspect ratio a = image_width / image_height, hence Sx = a * Sy.

    For a space:  area_m2 = Sx * Sy * normalized_polygon_area = a * Sy^2 * Anorm
              =>  Sy = sqrt(area_m2 / (a * Anorm)).

    We take the median Sy across all usable spaces for robustness.
    """
    w = meta.get("image_width")
    h = meta.get("image_height")
    aspect = (w / h) if (w and h) else 1.0

    sys_estimates: list[float] = []
    for sp in spaces:
        poly = sp.get("polygon_2d")
        area = sp.get("area_m2")
        if not poly or not area or len(poly) < 3:
            continue
        norm_area = Polygon(poly).area
        if norm_area <= 0:
            continue
        sys_estimates.append(math.sqrt(area / (aspect * norm_area)))

    if not sys_estimates:
        # No calibration possible -> assume the image already maps to metres.
        print("[graph_parser] WARNING: no area_m2 to calibrate scale; assuming 1:1.")
        return 1.0, 1.0

    sys_estimates.sort()
    sy = sys_estimates[len(sys_estimates) // 2]   # median
    sx = aspect * sy
    print(f"[graph_parser] Calibrated scale: Sx={sx:.3f} m/unit, Sy={sy:.3f} m/unit "
          f"(aspect={aspect:.3f}, from {len(sys_estimates)} spaces).")
    return sx, sy


def _to_metres_polygon(coords: list[list[float]], sx: float, sy: float) -> Polygon | None:
    """Convert a normalized polygon ring into a metric Shapely Polygon."""
    if not coords or len(coords) < 3:
        return None
    poly = Polygon(coords)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    # Scale about the origin so absolute positions stay consistent across objects.
    return shapely_scale(poly, xfact=sx, yfact=sy, origin=(0, 0))


def _bbox_to_metres_polygon(bbox: list[float], sx: float, sy: float) -> Polygon | None:
    """Convert a normalized [xmin, ymin, xmax, ymax] bbox into a metric Polygon."""
    if not bbox or len(bbox) != 4:
        return None
    xmin, ymin, xmax, ymax = bbox
    return box(xmin * sx, ymin * sy, xmax * sx, ymax * sy)


def _metric_dims(poly: Polygon) -> dict[str, float]:
    """Bounding-box width (shorter side), depth (longer side) and area in m²."""
    minx, miny, maxx, maxy = poly.bounds
    dx, dy = maxx - minx, maxy - miny
    return {
        "width": round(min(dx, dy), 4),
        "depth": round(max(dx, dy), 4),
        "area": round(poly.area, 4),
    }


# ---------------------------------------------------------------------------
# Connectivity (graph edges -> object links)
# ---------------------------------------------------------------------------
def _build_links(nodes: list[dict], edges: list[dict]) -> dict[str, set[str]]:
    """
    Build an undirected link map between object ids using:
      * every edge (from <-> to) between equipment nodes, and
      * every node <-> its space_id (membership).

    Result feeds GeometryObject.links so the validator can reason about
    room-to-room connectivity through the MEP/electrical topology.
    """
    links: dict[str, set[str]] = {}

    def add(a: str | None, b: str | None) -> None:
        if not a or not b or a == b:
            return
        links.setdefault(a, set()).add(b)
        links.setdefault(b, set()).add(a)

    for n in nodes:
        add(n.get("id"), n.get("space_id"))
    for e in edges:
        add(e.get("from"), e.get("to"))
    return links


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def parse_graph_dict(graph: dict) -> list[GeometryObject]:
    """Parse a unified-graph dictionary into standardized GeometryObjects."""
    meta = graph.get("meta", {})
    spaces = graph.get("spaces", []) or []
    nodes = graph.get("nodes", []) or []
    edges = graph.get("edges", []) or []

    sx, sy = _calibrate_scale(spaces, meta)
    links = _build_links(nodes, edges)
    objects: list[GeometryObject] = []

    # --- Spaces -> Rooms ---
    for sp in spaces:
        poly = _to_metres_polygon(sp.get("polygon_2d"), sx, sy)
        dims = _metric_dims(poly) if poly is not None else {}
        metrics = {
            # Use the authoritative area_m2 from the schema, not the derived one.
            "area": round(float(sp.get("area_m2", dims.get("area", 0.0))), 4),
            "width": dims.get("width", 0.0),
        }
        if sp.get("it_power_kW") is not None:
            metrics["it_power_kW"] = float(sp["it_power_kW"])
        objects.append(GeometryObject(
            id=sp["id"],
            **{"class": TargetClass.ROOM},
            type=sp.get("category"),
            geometry=poly,
            calculated_metrics=metrics,
            links=sorted(links.get(sp["id"], set())),
        ))

    # --- Nodes -> Racks / Equipment ---
    racks: list[GeometryObject] = []
    for n in nodes:
        poly = _bbox_to_metres_polygon(n.get("bbox_2d"), sx, sy)
        metrics = _metric_dims(poly) if poly is not None else {}
        # Copy numeric attributes (rated_power_kW, ampacity_A, ...) into metrics.
        for k, v in (n.get("attributes") or {}).items():
            if isinstance(v, (int, float)):
                metrics[k] = float(v)

        is_rack = n.get("type") == "rack"
        obj = GeometryObject(
            id=n["id"],
            **{"class": TargetClass.RACK if is_rack else TargetClass.EQUIPMENT},
            type=n.get("type"),
            geometry=poly,
            calculated_metrics=metrics,
            links=sorted(links.get(n["id"], set())),
        )
        objects.append(obj)
        if is_rack and poly is not None:
            racks.append(obj)

    # --- Derived aisles (gaps between parallel rack rows) ---
    objects.extend(_derive_aisles(racks))

    # --- Building-level synthetic object (PUE etc. from meta) ---
    building_metrics: dict[str, float] = {}
    for key in ("pue", "it_power_kW", "facility_power_kW", "target_pue"):
        if isinstance(meta.get(key), (int, float)):
            building_metrics[key] = float(meta[key])
    if building_metrics:
        objects.append(GeometryObject(
            id=meta.get("diagram_id", "building"),
            **{"class": TargetClass.BUILDING},
            type="Building",
            geometry=None,
            calculated_metrics=building_metrics,
        ))

    n_rooms = sum(1 for o in objects if o.cls == TargetClass.ROOM.value)
    n_equip = sum(1 for o in objects if o.cls == TargetClass.EQUIPMENT.value)
    n_aisle = sum(1 for o in objects if o.cls == TargetClass.AISLE.value)
    print(f"[graph_parser] Parsed {len(objects)} objects "
          f"({n_rooms} rooms, {len(racks)} racks, {n_equip} equipment, "
          f"{n_aisle} aisles, building metrics: {list(building_metrics)}).")
    return objects


def parse_graph_json(path: str) -> list[GeometryObject]:
    """Parse a unified-graph JSON file into standardized GeometryObjects."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Graph JSON not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        graph = json.load(fh)
    return parse_graph_dict(graph)
