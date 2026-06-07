"""
Parse datacenter floor plans / schematics using CV-hybrid Set-of-Marks.

Examples:
  python scripts/parse_datacenter.py "data/datacenter/preview (2).webp" --style serverroom
  python scripts/parse_datacenter.py --all
  python scripts/parse_datacenter.py "data/datacenter/preview.webp" --debug-marks data/dc_marks_datahall.png
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DC_DIR = ROOT / "data" / "datacenter"
ALL_MAP = [
    ("preview.webp", "datahall"),
    ("preview (2).webp", "serverroom"),
    ("preview (1).webp", "cooling"),
]


def _normalize_graph_for_viz(graph):
    """Map DC-specific keys to spaces/nodes for visualize.py."""
    out = dict(graph)
    if "zones" in out and "spaces" not in out:
        out["spaces"] = out["zones"]
    if "equipment" in out and "nodes" not in out:
        nodes = []
        for eq in out["equipment"]:
            nodes.append(
                {
                    "id": eq["id"],
                    "type": eq.get("type", "unknown"),
                    "tag": eq.get("tag", eq["id"]),
                    "bbox_2d": eq.get("bbox_2d"),
                    "confidence": eq.get("confidence", 0.8),
                }
            )
        out["nodes"] = nodes
    out.setdefault("edges", graph.get("edges") or [])
    if graph.get("annotations"):
        out["annotations"] = graph["annotations"]
    return out


def main():
    ap = argparse.ArgumentParser(description="Parse datacenter plans (CV-hybrid)")
    ap.add_argument("image", nargs="?", help="Input image (webp/png)")
    ap.add_argument("--style", default="auto", choices=["auto", "serverroom", "datahall", "cooling"])
    ap.add_argument("--out", default=None, help="Output JSON path")
    ap.add_argument("--overlay", default=None, help="Output overlay PNG path")
    ap.add_argument("--debug-marks", default=None, help="Save numbered marks image")
    ap.add_argument("--all", action="store_true", help="Parse all data/datacenter/*.webp")
    ap.add_argument("--model", default=None)
    ap.add_argument("--handwriting", action="store_true", help="OCR handwritten/red-ink notes into annotations[]")
    args = ap.parse_args()

    from src.run_utils import load_src

    dc_mod = load_src("08_datacenter.py")
    viz_mod = None

    jobs = []
    if args.all:
        for fname, style in ALL_MAP:
            path = DC_DIR / fname
            if path.exists():
                jobs.append((path, style))
        if not jobs:
            print("No files in", DC_DIR)
            return
    elif args.image:
        path = Path(args.image)
        style = dc_mod.detect_style(path, args.style)
        jobs = [(path, style)]
    else:
        ap.print_help()
        return

    for path, style in jobs:
        print(f"Parsing {path.name} as {style}...")
        kwargs = {"style": style, "handwriting": args.handwriting}
        if args.model:
            kwargs["model"] = args.model
        if args.debug_marks:
            kwargs["debug_marks_path"] = args.debug_marks

        graph = dc_mod.parse_datacenter(str(path), **kwargs)

        out = Path(args.out) if args.out else ROOT / "data" / f"parsed_dc_{style}.json"
        if args.all:
            out = ROOT / "data" / f"parsed_dc_{style}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(graph, f, indent=2)
        print(f"  Wrote {out} | parser={graph['meta'].get('parser')}")

        overlay = Path(args.overlay) if args.overlay else ROOT / "data" / f"overlay_dc_{style}.png"
        if args.all:
            overlay = ROOT / "data" / f"overlay_dc_{style}.png"
        if viz_mod is None:
            import importlib.util

            spec = importlib.util.spec_from_file_location("viz", ROOT / "scripts" / "visualize.py")
            viz_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(viz_mod)
        viz_graph = _normalize_graph_for_viz(graph)
        viz_mod.overlay_graph(str(path), viz_graph, out_path=str(overlay))
        print(f"  Wrote {overlay}")


if __name__ == "__main__":
    main()
