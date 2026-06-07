"""
Turn detections + OCR into graph JSON matching schemas/graph.schema.json.
Simple edge guess: connect nodes whose centers are close (same sheet, no real line tracing yet).
"""

import argparse
import json
import math
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCHEMA_PATH = ROOT / "schemas" / "graph.schema.json"
CLASS_MAP_PATH = ROOT / "config" / "class_map.yaml"
if not CLASS_MAP_PATH.exists():
    CLASS_MAP_PATH = ROOT / "class_map.yaml"


def load_class_map(path=None):
    if path is None:
        path = CLASS_MAP_PATH
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def map_sub_type_to_type(sub_type, class_map):
    if sub_type in class_map:
        return class_map[sub_type]
    if sub_type.startswith("equipment.pump"):
        return "pump"
    if sub_type.startswith("tower."):
        return "cooling_tower"
    if sub_type.startswith("valve."):
        return "valve"
    if sub_type.startswith("instrument."):
        return "instrument"
    if sub_type.startswith("equipment.centrifugal_blower") or sub_type.startswith(
        "equipment.blower"
    ):
        return "crac"
    return "unknown"


def center_from_bbox(bbox_2d):
    return bbox_2d[0], bbox_2d[1]


def distance(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def guess_edge_type(node_a, node_b):
    ta = node_a["type"]
    tb = node_b["type"]
    if ta in ("transformer", "breaker", "distribution_panel", "meter") or tb in (
        "transformer",
        "breaker",
        "distribution_panel",
        "meter",
    ):
        return "electrical_cable"
    if ta == "pump" or tb == "pump" or ta == "chiller" or tb == "chiller":
        return "chw_supply"
    if ta == "cooling_tower" or tb == "cooling_tower":
        return "condenser_water"
    if ta == "valve" or tb == "valve":
        return "chw_supply"
    return "unknown"


def build_edges(nodes, max_dist=0.12):
    """Connect each node to its nearest neighbor if close enough (normalized coords)."""
    edges = []
    used_pairs = set()
    edge_id = 1

    for i, na in enumerate(nodes):
        best_j = None
        best_d = max_dist
        ca = center_from_bbox(na["bbox_2d"])
        for j, nb in enumerate(nodes):
            if i == j:
                continue
            cb = center_from_bbox(nb["bbox_2d"])
            d = distance(ca, cb)
            if d < best_d:
                best_d = d
                best_j = j
        if best_j is None:
            continue
        pair = tuple(sorted([na["id"], nodes[best_j]["id"]]))
        if pair in used_pairs:
            continue
        used_pairs.add(pair)
        nb = nodes[best_j]
        etype = guess_edge_type(na, nb)
        ca = center_from_bbox(na["bbox_2d"])
        cb = center_from_bbox(nb["bbox_2d"])
        edges.append(
            {
                "id": "E" + str(edge_id),
                "type": etype,
                "from": na["id"],
                "to": nb["id"],
                "attributes": {},
                "polyline_2d": [list(ca), list(cb)],
                "confidence": 0.4,
            }
        )
        edge_id += 1

    return edges


def build_graph(preprocess_result, detections, diagram_type="PID", diagram_id="parsed-01"):
    class_map = load_class_map()
    nodes = []
    confidences = []

    for i, det in enumerate(detections):
        sub_type = det.get("sub_type") or "unknown"
        eng_type = map_sub_type_to_type(sub_type, class_map)
        attrs = det.get("parsed_attrs") or {}
        if not isinstance(attrs, dict):
            attrs = {}

        node = {
            "id": "N" + str(i + 1),
            "type": eng_type,
            "sub_type": sub_type,
            "tag": det.get("tag") or "",
            "attributes": attrs,
            "bbox_2d": det["bbox_2d"],
            "confidence": det.get("confidence", 0.5),
        }
        nodes.append(node)
        confidences.append(det.get("confidence", 0.5))

    if not nodes:
        raise ValueError("No nodes — run detection first or lower conf threshold")

    edges = build_edges(nodes)

    avg_conf = sum(confidences) / len(confidences) if confidences else 0

    warnings = []
    if len(nodes) > 1 and len(edges) < len(nodes) - 1:
        warnings.append(
            "sparse_edges: connectivity is first-order proximity only, not traced pipe lines"
        )
    if any(n["type"] == "unknown" for n in nodes):
        warnings.append("unknown_node_types: some detections did not map to engineering types")

    graph = {
        "meta": {
            "diagram_id": diagram_id,
            "diagram_type": diagram_type,
            "building_id": None,
            "source_file": preprocess_result["source_file"],
            "image_width": preprocess_result["width"],
            "image_height": preprocess_result["height"],
            "parse_confidence": round(avg_conf, 3),
            "topology": "first_order_proximity",
            "warnings": warnings,
        },
        "nodes": nodes,
        "edges": edges,
    }
    return graph


def validate_graph(graph, schema_path=None):
    import jsonschema

    if schema_path is None:
        schema_path = SCHEMA_PATH
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(graph, schema)
    return True


def parse_diagram(path, weights_path=None, diagram_type="PID", conf=0.15):

    from src import preprocess as prep_mod
    from src import detect_yolo as det_mod
    from src import extract_text as ocr_mod

    prep = prep_mod.preprocess(path)
    dets = det_mod.detect(prep, weights_path=weights_path, conf=conf)
    dets = ocr_mod.extract_for_detections(prep, dets)
    graph = build_graph(prep, dets, diagram_type=diagram_type)
    validate_graph(graph)
    return graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?", default=None)
    parser.add_argument("--out", default=str(ROOT / "data" / "demo_graph.json"))
    parser.add_argument("--weights", default=None)
    parser.add_argument("--conf", type=float, default=0.15)
    parser.add_argument("--type", default="PID", choices=["PID", "SLD"])
    args = parser.parse_args()

    if args.image:
        path = args.image
    else:
        raw = ROOT / "data" / "raw_diagrams"
        files = list(raw.glob("*.*"))
        if not files:
            print("Put a file in data/raw_diagrams/")
            return
        path = str(files[0])

    graph = parse_diagram(
        path, weights_path=args.weights, diagram_type=args.type, conf=args.conf
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    print("Wrote", out, "nodes:", len(graph["nodes"]), "edges:", len(graph["edges"]))


if __name__ == "__main__":
    main()
