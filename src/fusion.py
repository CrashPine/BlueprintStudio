"""
Fusion layer: merge architectural + MEP partial graphs; assign space_id via point-in-polygon.
"""

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def detect_diagram_class(image_path=None, diagram_type_hint=None):
    """
    Route diagram to FLOORPLAN vs PID vs SLD.
    Explicit hint wins; else filename/heuristic.
    """
    if diagram_type_hint and diagram_type_hint.upper() in (
        "FLOORPLAN",
        "PID",
        "SLD",
        "FUSED",
        "AUTO",
    ):
        hint = diagram_type_hint.upper()
        if hint != "AUTO":
            if hint == "FUSED":
                return "FUSED"
            return hint

    if image_path:
        name = Path(image_path).name.lower()
        if any(k in name for k in ("floor", "plan", "layout", "arch", "dc_layout")):
            return "FLOORPLAN"
        if any(k in name for k in ("sld", "single_line", "electrical", "power")):
            return "SLD"
        if any(k in name for k in ("pid", "p&id", "hvac", "cooling", "chiller")):
            return "PID"

    return "PID"


def _node_center(node):
    bbox = node.get("bbox_2d")
    if bbox and len(bbox) >= 2:
        return bbox[0], bbox[1]
    return None


def assign_space_ids(nodes, spaces):
    """Point-in-polygon: bbox center -> space_id."""
    try:
        from shapely.geometry import Point, Polygon
    except ImportError:
        return nodes

    polys = []
    for sp in spaces:
        pts = sp.get("polygon_2d") or []
        if len(pts) < 3:
            continue
        try:
            polys.append((sp["id"], Polygon(pts)))
        except Exception:
            continue

    for node in nodes:
        center = _node_center(node)
        if not center:
            continue
        pt = Point(center)
        best_id = None
        for sid, poly in polys:
            if poly.contains(pt) or poly.distance(pt) < 0.02:
                best_id = sid
                break
        if best_id:
            node["space_id"] = best_id
    return nodes


def fuse_graphs(arch_graph, mep_graph, diagram_id="fused-01"):
    """Merge floor-plan spaces/walls with MEP nodes/edges into FUSED graph."""
    arch = copy.deepcopy(arch_graph)
    mep = copy.deepcopy(mep_graph)

    spaces = arch.get("spaces") or []
    walls = arch.get("walls") or []
    nodes = mep.get("nodes") or []
    edges = mep.get("edges") or []

    assign_space_ids(nodes, spaces)

    meta = {
        "diagram_id": diagram_id,
        "diagram_type": "FUSED",
        "building_id": arch.get("meta", {}).get("building_id")
        or mep.get("meta", {}).get("building_id"),
        "source_file": mep.get("meta", {}).get("source_file")
        or arch.get("meta", {}).get("source_file")
        or "fused",
        "image_width": arch.get("meta", {}).get("image_width")
        or mep.get("meta", {}).get("image_width"),
        "image_height": arch.get("meta", {}).get("image_height")
        or mep.get("meta", {}).get("image_height"),
        "parse_confidence": round(
            (
                arch.get("meta", {}).get("parse_confidence", 0.5)
                + mep.get("meta", {}).get("parse_confidence", 0.5)
            )
            / 2,
            3,
        ),
        "topology": "arch_mep_fusion",
        "warnings": list(arch.get("meta", {}).get("warnings") or [])
        + list(mep.get("meta", {}).get("warnings") or []),
        "room_count": len(spaces),
        "total_floor_area_m2": round(
            sum(float(s.get("area_m2") or 0) for s in spaces), 1
        ),
    }
    arch_scale = arch.get("meta", {}).get("scale_m_per_px")
    if arch_scale:
        meta["scale_m_per_px"] = arch_scale

    fused = {
        "meta": meta,
        "spaces": spaces,
        "walls": walls,
        "nodes": nodes,
        "edges": edges,
    }
    return fused


def _parse_legacy(path, kind, conf, mep_type):
    """YOLO/OpenCV pipeline (offline fallback)."""

    if kind == "FLOORPLAN":
        from src import parse_floorplan as fp_mod
        graph = fp_mod.parse_floorplan(path, conf=conf)
        graph["meta"]["parser"] = "floorplan-cv"
        return graph

    from src import build_graph as build_mod
    dt = mep_type if kind == "AUTO" else kind
    if dt not in ("PID", "SLD"):
        dt = "PID"
    graph = build_mod.parse_diagram(path, diagram_type=dt, conf=conf)
    graph["meta"]["parser"] = "yolo"
    return graph


def parse_unified(path, diagram_type="AUTO", conf=0.15, mep_type="PID", engine="claude", handwriting=False):
    """
    Single entry: route to floor-plan or MEP parser.
    engine: claude (default, falls back on failure) | cv-hybrid (Roboflow rooms +
            Claude for floor plans) | yolo (legacy only) | auto (alias of claude)
    """

    kind = detect_diagram_class(path, diagram_type)

    if engine in ("claude", "auto", "cv-hybrid"):
        # Floor plans use the Roboflow + Claude CV-hybrid (pixel-accurate room
        # boxes from a trained detector); it falls back to Claude-only internally.
        if kind == "FLOORPLAN":
            try:
                from src import floorplan_hybrid as fp_mod
                # Use the module's calibrated detection confidence (the `conf`
                # arg here is the legacy YOLO threshold, not the Roboflow one).
                return fp_mod.parse_floorplan_hybrid(path, handwriting=handwriting)
            except Exception as exc:
                graph = _parse_legacy(path, kind, conf, mep_type)
                warn = f"hybrid_fallback: {type(exc).__name__}: {exc}"
                graph.setdefault("meta", {}).setdefault("warnings", []).append(warn)
                return graph

        # MEP (P&ID / SLD): deterministic YOLO boxes + Claude topology/OCR.
        mep_kind = mep_type if kind == "AUTO" else kind
        if mep_kind not in ("PID", "SLD"):
            mep_kind = "PID"
        try:
            from src import pid_hybrid as pid_mod
            graph = pid_mod.parse_pid_hybrid(
                path, diagram_type=mep_kind, conf=conf, handwriting=handwriting
            )
            if graph.get("nodes"):
                return graph
            # No MEP symbols detected — likely a datacenter/space layout.
            return _parse_datacenter_fallback(path, handwriting, graph)
        except Exception as exc:
            dc = _try_datacenter(path, handwriting)
            if dc is not None:
                dc.setdefault("meta", {}).setdefault("warnings", []).append(
                    f"pid_hybrid_fallback: {type(exc).__name__}: {exc}"
                )
                return dc
            graph = _parse_legacy(path, kind, conf, mep_type)
            warn = f"pid_hybrid_fallback: {type(exc).__name__}: {exc}"
            graph.setdefault("meta", {}).setdefault("warnings", []).append(warn)
            return graph

    return _parse_legacy(path, kind, conf, mep_type)


def _try_datacenter(path, handwriting):
    """Best-effort datacenter (OpenCV regions + Claude) parse; None on failure."""
    try:
        from src import datacenter as dc_mod
        return dc_mod.parse_datacenter(path, handwriting=handwriting)
    except Exception:
        return None


def _parse_datacenter_fallback(path, handwriting, pid_graph):
    """When the MEP detector finds no nodes, try the datacenter parser."""
    dc = _try_datacenter(path, handwriting)
    if dc is not None and (dc.get("spaces") or dc.get("nodes") or dc.get("zones")):
        dc.setdefault("meta", {}).setdefault("warnings", []).append(
            "routed_to_datacenter: no MEP symbols detected"
        )
        return dc
    return pid_graph
