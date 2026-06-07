"""
FlowDraft / ArchDraft — production inference entry point.

Parse any supported building diagram into schema-valid JSON using the
deterministic CV + Claude hybrid pipelines (Roboflow rooms/fixtures, local
YOLO MEP symbols, OpenCV datacenter marks, optional handwriting OCR).

Run from the repository root:

    python scripts/infer.py <image> --type AUTO --out data/parsed.json
    python scripts/infer.py plan.png --type FLOORPLAN --handwriting --overlay data/overlay.png
    python scripts/infer.py pid.jpg --type PID --weights models/yolov8n_pid.pt
    python scripts/infer.py data/datacenter/preview.webp --type DC_DATAHALL

API keys (first match wins):
  1. CLI flags --anthropic-key / --roboflow-key
  2. Environment variables ANTHROPIC_API_KEY / ROBOFLOW_API_KEY
  3. models/secretapi.txt and models/roboflow_key.txt (see src/secrets_util.py)

Programmatic:

    from scripts.infer import InferConfig, parse_document

    graph = parse_document(
        "plan.png",
        InferConfig(
            yolo_weights="models/yolov8n_pid.pt",
            anthropic_api_key="sk-ant-...",
            roboflow_api_key="rf_...",
            schema_path="schemas/graph.schema.json",
        ),
        diagram_type="FLOORPLAN",
        handwriting=True,
    )
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_DIR = ROOT / "schemas"
DEFAULT_GRAPH_SCHEMA = SCHEMA_DIR / "graph.schema.json"

# diagram_type -> validation schema (when not overridden)
_SCHEMA_BY_DIAGRAM_TYPE = {
    "FLOORPLAN": DEFAULT_GRAPH_SCHEMA,
    "PID": DEFAULT_GRAPH_SCHEMA,
    "SLD": DEFAULT_GRAPH_SCHEMA,
    "FUSED": DEFAULT_GRAPH_SCHEMA,
    "DC_DATAHALL": SCHEMA_DIR / "dc_datahall.schema.json",
    "DC_SERVERROOM": SCHEMA_DIR / "dc_serverroom.schema.json",
    "COOLING_PID": SCHEMA_DIR / "cooling_pid.schema.json",
}

_DC_TYPES = frozenset({"DC_DATAHALL", "DC_SERVERROOM", "COOLING_PID"})
_DC_STYLE = {
    "DC_DATAHALL": "datahall",
    "DC_SERVERROOM": "serverroom",
    "COOLING_PID": "cooling",
}


@dataclass
class InferConfig:
    """Runtime configuration for inference."""

    yolo_weights: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    roboflow_api_key: Optional[str] = None
    schema_path: Optional[str] = None
    claude_model: str = "claude-sonnet-4-6"
    engine: str = "cv-hybrid"
    conf: float = 0.15

    def apply(self) -> None:
        """Push API keys into the environment so secrets_util picks them up."""
        if self.anthropic_api_key:
            os.environ["ANTHROPIC_API_KEY"] = self.anthropic_api_key.strip()
        if self.roboflow_api_key:
            os.environ["ROBOFLOW_API_KEY"] = self.roboflow_api_key.strip()


def _resolve_schema(graph: dict, config: InferConfig, diagram_type: str) -> Path:
    if config.schema_path:
        return Path(config.schema_path)
    meta_type = (graph.get("meta") or {}).get("diagram_type") or diagram_type
    path = _SCHEMA_BY_DIAGRAM_TYPE.get(str(meta_type).upper())
    return path or DEFAULT_GRAPH_SCHEMA


def validate_graph(graph: dict, schema_path: Path | str) -> None:
    import jsonschema

    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(graph, schema)


def _guess_datacenter(path: str) -> bool:
    name = Path(path).as_posix().lower()
    return any(
        k in name
        for k in ("datacenter/", "datacentre/", "/dc_", "preview.webp", "preview (")
    )


def parse_document(
    image_path: str | Path,
    config: Optional[InferConfig] = None,
    *,
    diagram_type: str = "AUTO",
    handwriting: bool = False,
    dc_style: str = "auto",
    debug_marks_path: Optional[str] = None,
    validate: bool = True,
) -> dict[str, Any]:
    """
    Parse an image into a graph dict.

    diagram_type: AUTO | FLOORPLAN | PID | SLD | DC_DATAHALL | DC_SERVERROOM | COOLING_PID
    handwriting:  when True, OCR handwritten/red-ink notes into annotations[]
    dc_style:     datahall | serverroom | cooling | auto (datacenter only)
    """
    from src.run_utils import load_src

    config = config or InferConfig()
    config.apply()

    path = str(image_path)
    kind = diagram_type.upper()

    if kind in _DC_TYPES or (kind == "AUTO" and _guess_datacenter(path)):
        dc_mod = load_src("08_datacenter.py")
        style = _DC_STYLE.get(kind, dc_style)
        graph = dc_mod.parse_datacenter(
            path,
            style=style,
            model=config.claude_model,
            debug_marks_path=debug_marks_path,
            handwriting=handwriting,
        )
    elif kind == "FLOORPLAN" or (kind == "AUTO" and _guess_floorplan(path)):
        fp_mod = load_src("09_floorplan_hybrid.py")
        graph = fp_mod.parse_floorplan_hybrid(
            path,
            conf=config.conf,
            model=config.claude_model,
            debug_marks_path=debug_marks_path,
            handwriting=handwriting,
        )
    elif kind in ("PID", "SLD", "AUTO"):
        pid_mod = load_src("10_pid_hybrid.py")
        mep_type = "PID" if kind == "AUTO" else kind
        graph = pid_mod.parse_pid_hybrid(
            path,
            diagram_type=mep_type,
            conf=config.conf,
            model=config.claude_model,
            weights_path=config.yolo_weights,
            debug_marks_path=debug_marks_path,
            handwriting=handwriting,
        )
    else:
        fusion = load_src("06_fusion.py")
        graph = fusion.parse_unified(
            path,
            diagram_type=kind,
            conf=config.conf,
            engine=config.engine,
            handwriting=handwriting,
        )

    if validate:
        schema = _resolve_schema(graph, config, kind)
        validate_graph(graph, schema)

    return graph


def _guess_floorplan(path: str) -> bool:
    name = Path(path).name.lower()
    return any(
        k in name
        for k in ("floor", "plan", "layout", "arch", "fp_", "screenshot", "img_")
    ) and not any(k in name for k in ("pid", "p&id", "hvac", "sld"))


def render_overlay(
    image_path: str | Path,
    graph: dict,
    out_path: str | Path,
    *,
    max_labels: Optional[int] = 60,
) -> str:
    """Draw parsed graph on source image; returns output path."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("viz", ROOT / "scripts" / "visualize.py")
    viz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(viz)
    return viz.overlay_graph(str(image_path), graph, out_path=str(out_path), max_labels=max_labels)


def _summary(graph: dict) -> str:
    meta = graph.get("meta") or {}
    parts = [
        f"parser={meta.get('parser')}",
        f"type={meta.get('diagram_type')}",
        f"spaces={len(graph.get('spaces') or graph.get('zones') or [])}",
        f"fixtures={len(graph.get('fixtures') or [])}",
        f"nodes={len(graph.get('nodes') or graph.get('equipment') or [])}",
        f"edges={len(graph.get('edges') or [])}",
        f"annotations={len(graph.get('annotations') or [])}",
    ]
    return " | ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="FlowDraft production inference (CV-hybrid + schema validation)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("image", help="Input PNG/JPG/WEBP")
    ap.add_argument(
        "--type",
        default="AUTO",
        choices=[
            "AUTO", "FLOORPLAN", "PID", "SLD",
            "DC_DATAHALL", "DC_SERVERROOM", "COOLING_PID",
        ],
        help="Diagram kind (AUTO uses filename heuristics)",
    )
    ap.add_argument("--out", default=None, help="Output JSON path")
    ap.add_argument("--overlay", default=None, help="Optional overlay PNG path")
    ap.add_argument("--weights", default=None, help="YOLO weights for P&ID (e.g. models/yolov8n_pid.pt)")
    ap.add_argument("--schema", default=None, help="JSON schema for validation (default: auto by diagram type)")
    ap.add_argument("--anthropic-key", default=None, help="Anthropic API key (or use env / models/secretapi.txt)")
    ap.add_argument("--roboflow-key", default=None, help="Roboflow API key (or use env / models/roboflow_key.txt)")
    ap.add_argument("--model", default="claude-sonnet-4-6", help="Claude model id")
    ap.add_argument("--engine", default="cv-hybrid", choices=["cv-hybrid", "claude", "yolo", "auto"])
    ap.add_argument("--conf", type=float, default=0.15, help="YOLO confidence threshold")
    ap.add_argument("--handwriting", action="store_true", help="OCR handwritten notes into annotations[]")
    ap.add_argument("--dc-style", default="auto", choices=["auto", "datahall", "serverroom", "cooling"])
    ap.add_argument("--debug-marks", default=None, help="Save numbered CV marks image")
    ap.add_argument("--no-validate", action="store_true", help="Skip jsonschema validation")
    ap.add_argument("--max-labels", type=int, default=60, help="Cap labels on overlay (dense P&IDs)")
    args = ap.parse_args()

    image = Path(args.image)
    if not image.exists():
        raise SystemExit(f"Image not found: {image}")

    cfg = InferConfig(
        yolo_weights=args.weights,
        anthropic_api_key=args.anthropic_key,
        roboflow_api_key=args.roboflow_key,
        schema_path=args.schema,
        claude_model=args.model,
        engine=args.engine,
        conf=args.conf,
    )

    graph = parse_document(
        image,
        cfg,
        diagram_type=args.type,
        handwriting=args.handwriting,
        dc_style=args.dc_style,
        debug_marks_path=args.debug_marks,
        validate=not args.no_validate,
    )

    out = Path(args.out) if args.out else ROOT / "data" / f"parsed_{image.stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    print(f"Wrote {out}")
    print(_summary(graph))

    if args.overlay:
        overlay_path = render_overlay(image, graph, args.overlay, max_labels=args.max_labels)
        print(f"Wrote {overlay_path}")


if __name__ == "__main__":
    main()
