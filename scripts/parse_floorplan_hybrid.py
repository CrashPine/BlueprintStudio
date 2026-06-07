"""
Parse a floor plan with the Roboflow + Claude CV-hybrid pipeline.

Roboflow `architectural-blueprint/2` supplies pixel-accurate room boxes;
Claude labels the numbered marks (names, categories, verbatim areas).

Examples:
  python scripts/parse_floorplan_hybrid.py data/raw_floorplans/13.png
  python scripts/parse_floorplan_hybrid.py data/raw_floorplans/13.png --debug-marks data/fp_marks_13.png --overlay data/overlay_13_hybrid.png
  python scripts/parse_floorplan_hybrid.py --all
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FP_DIR = ROOT / "data" / "raw_floorplans"


def main():
    ap = argparse.ArgumentParser(description="Parse a floor plan (Roboflow + Claude CV-hybrid)")
    ap.add_argument("image", nargs="?", help="Input floor-plan image (png/jpg/webp)")
    ap.add_argument("--out", default=None, help="Output JSON path")
    ap.add_argument("--overlay", default=None, help="Output overlay PNG path")
    ap.add_argument("--debug-marks", default=None, help="Save numbered marks image")
    ap.add_argument("--conf", type=float, default=0.25, help="Roboflow confidence threshold")
    ap.add_argument("--all", action="store_true", help="Parse all data/raw_floorplans/* images")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    from src.run_utils import load_src

    fp_mod = load_src("09_floorplan_hybrid.py")
    viz_mod = None

    jobs = []
    if args.all:
        for p in sorted(FP_DIR.glob("*")):
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                jobs.append(p)
        if not jobs:
            print("No images in", FP_DIR)
            return
    elif args.image:
        jobs = [Path(args.image)]
    else:
        ap.print_help()
        return

    for path in jobs:
        print(f"Parsing {path.name}...")
        kwargs = {"conf": args.conf}
        if args.model:
            kwargs["model"] = args.model
        if args.debug_marks and not args.all:
            kwargs["debug_marks_path"] = args.debug_marks
        elif args.all:
            kwargs["debug_marks_path"] = str(ROOT / "data" / f"fp_marks_{path.stem}.png")

        graph = fp_mod.parse_floorplan_hybrid(str(path), **kwargs)

        out = Path(args.out) if (args.out and not args.all) else ROOT / "data" / f"parsed_floor_{path.stem}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(graph, f, indent=2)
        print(
            f"  Wrote {out} | parser={graph['meta'].get('parser')} "
            f"| spaces={len(graph.get('spaces') or [])}"
        )

        overlay = (
            Path(args.overlay)
            if (args.overlay and not args.all)
            else ROOT / "data" / f"overlay_{path.stem}_hybrid.png"
        )
        if viz_mod is None:
            import importlib.util

            spec = importlib.util.spec_from_file_location("viz", ROOT / "scripts" / "visualize.py")
            viz_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(viz_mod)
        viz_mod.overlay_graph(str(path), graph, out_path=str(overlay))
        print(f"  Wrote {overlay}")


if __name__ == "__main__":
    main()
