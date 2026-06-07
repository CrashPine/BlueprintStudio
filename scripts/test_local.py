"""
Offline smoke tests for the v3 cherry-pick (no network / no Claude calls).

Run from repo root:
    python scripts/test_local.py
"""

import copy
import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.run_utils import load_src  # noqa: E402

cp = load_src("07_claude_parse.py")
pue_mod = load_src("pue.py")
loads_mod = load_src("loads.py")
STRICT = json.loads((ROOT / "schemas" / "graph.schema.json").read_text(encoding="utf-8"))


def _ok(msg):
    print(f"  OK: {msg}")


def test_schema_and_enrichment():
    floor = json.loads((ROOT / "data" / "parsed_floor_13.json").read_text(encoding="utf-8"))
    jsonschema.validate(floor, STRICT)
    _ok("parsed_floor_13.json validates as-is")

    meta = floor.get("meta") or {}
    w = meta.get("image_width") or 1000
    h = meta.get("image_height") or 1000
    fresh = copy.deepcopy(floor)
    for sp in fresh.get("spaces") or []:
        for k in ("bbox_2d", "centroid_2d", "width_m", "height_m", "area_source"):
            sp.pop(k, None)
    out = cp._finalize(fresh, "13.png", w, h, "FLOORPLAN")
    jsonschema.validate(out, STRICT)
    m = out["meta"]
    assert m.get("scale_m_per_px"), "expected scale_m_per_px after enrichment"
    assert m.get("unit_system") in ("metric", "imperial", "mixed")
    _ok(f"re-enriched: scale={m.get('scale_m_per_px')}, rooms={m.get('room_count')}")


def test_measurement_helpers():
    assert cp._to_float("11,20 m2") == 11.2
    assert cp._parse_area_to_m2("465 SQ FT") == 43.2
    assert cp._parse_dimensions("4.50 x 3.20") == (4.5, 3.2)
    _ok("measurement helpers")


def test_pue_loads_fallbacks():
    dc = json.loads((ROOT / "data" / "demo_datacentre.json").read_text(encoding="utf-8"))
    pue_v2 = pue_mod.compute_pue(dc)
    loads_v2 = loads_mod.analyze(dc)

    dc_v3 = copy.deepcopy(dc)
    for n in dc_v3.get("nodes") or []:
        a = n.get("attributes") or {}
        if "rated_power_kW" in a:
            a["power_kW"] = a.pop("rated_power_kW")
        if "ampacity_A" in a:
            a["current_A"] = a.pop("ampacity_A")
    pue_v3 = pue_mod.compute_pue(dc_v3)
    loads_v3 = loads_mod.analyze(dc_v3)

    assert pue_v2 == pue_v3
    assert loads_v2["total_facility_load_kW"] == loads_v3["total_facility_load_kW"]
    _ok(f"PUE={pue_v2['pue']}, facility={pue_v2['facility_power_kW']} kW (v2/v3 names match)")


def test_visualizer_smoke():
    import importlib.util
    import cv2
    import numpy as np

    spec = importlib.util.spec_from_file_location("viz", ROOT / "scripts" / "visualize.py")
    viz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(viz)

    floor = json.loads((ROOT / "data" / "parsed_floor_13.json").read_text(encoding="utf-8"))
    meta = floor.get("meta") or {}
    iw, ih = int(meta.get("image_width") or 1200), int(meta.get("image_height") or 900)
    canvas = np.full((ih, iw, 3), 245, dtype=np.uint8)
    img_path = ROOT / "data" / "_test_canvas.png"
    out_path = ROOT / "data" / "_test_overlay.png"
    cv2.imwrite(str(img_path), canvas)
    try:
        res = viz.overlay_graph(str(img_path), floor, out_path=str(out_path))
        assert Path(res).stat().st_size > 0
        _ok(f"visualizer wrote {Path(res).name} (PIL={viz._PIL_OK})")
    finally:
        img_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


def main():
    print("Running offline v3 cherry-pick tests...")
    test_schema_and_enrichment()
    test_measurement_helpers()
    test_pue_loads_fallbacks()
    test_visualizer_smoke()
    print("\nALL OFFLINE TESTS PASSED")


if __name__ == "__main__":
    main()
