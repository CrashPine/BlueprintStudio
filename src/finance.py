"""
Finance module: estimates Hong Kong property value and annual ROI
from a parsed floor-plan graph and market datasets.

Public API
----------
load_datasets()                                    -> nested dict, all district price/rent data
list_districts()                                   -> list[str]
list_estates(district)                             -> list[str]
estimate_from_area_sqft(area_sqft, district, ...)  -> dict
estimate_from_graph(graph, district, ...)          -> dict

ROI formula
-----------
  area_sqft      = area_m2 * M2_TO_SQFT
  property_value = area_sqft * price_per_sqft           [HKD]
  annual_benefit = rent_per_sqft_month * area_sqft * 12 [HKD/year]
  annual_roi_pct = annual_benefit / property_value * 100 [%]

CLI
---
  cd eurotech
  py -m src.finance image data/raw_floorplans/118.png --district "Kowloon"

  cd eurotech/src
  py -m finance image ../data/raw_floorplans/118.png --district "Kowloon"
  py -m finance area 75.5 --district "Hong Kong Island" --estate "Larvotto"
  py -m finance list --district "New Territories"

Note: the 'image' subcommand requires opencv-python and anthropic to be installed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional

# ─── paths ───────────────────────────────────────────────────────────────────

_SRC = Path(__file__).resolve().parent          # eurotech/src/
ROOT = _SRC.parent                              # eurotech/
_PROJECT = ROOT.parent                          # coupdegrace/

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _resolve_data_dir() -> Path:
    """Find property price datasets (config/property_prices/ or legacy paths)."""
    env = os.environ.get("FLOWDRAFT_PROPERTY_DATA")
    if env:
        p = Path(env)
        if p.is_dir():
            return p
    for candidate in (
        ROOT / "config" / "property_prices",
        ROOT / "average_property_price",
        _PROJECT / "average_property_price",
    ):
        if candidate.is_dir():
            return candidate
    return ROOT / "config" / "property_prices"


DATA_DIR = _resolve_data_dir()

# ─── constants ───────────────────────────────────────────────────────────────

M2_TO_SQFT = 10.7639  # 1 m² = 10.7639 ft²

# Maps human-readable district name -> (price_file, rent_file) relative to DATA_DIR.
_DISTRICT_FILES: dict[str, tuple[str, str]] = {
    "Hong Kong Island": ("Hong_Kong_Island.txt", "Hong_Kong_Island_Rent.txt"),
    "Kowloon":          ("Kowloon.txt",           "Kowloon_rent.txt"),
    "New Territories":  ("New_Territories.txt",   "New_Territories_rent.txt"),
}

_datasets_cache: Optional[dict] = None


# ─── dataset parsing ─────────────────────────────────────────────────────────

def _parse_number(s: str) -> Optional[float]:
    """'17,316' / '45.0' / '' -> float or None."""
    cleaned = s.strip().replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_md_table(path: Path) -> list[dict[str, str]]:
    """Read a pipe-delimited markdown table; return list of row dicts."""
    rows: list[dict[str, str]] = []
    headers: Optional[list[str]] = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if headers is None:
                headers = cells
                continue
            # skip separator row (dashes / colons only)
            if all(re.fullmatch(r"[-: ]+", c) for c in cells):
                continue
            if len(cells) == len(headers):
                rows.append(dict(zip(headers, cells)))
    return rows


def _load_district(price_path: Path, rent_path: Path) -> dict[str, dict]:
    """
    Merge price and rent tables for one district.

    Returns
    -------
    {estate_name: {"price_hkd_sqft": float | None, "rent_hkd_sqft_month": float | None}}
    """
    data: dict[str, dict] = {}

    for row in _parse_md_table(price_path):
        estate = row.get("Estate", "").strip()
        if not estate:
            continue
        price_raw = next((v for k, v in row.items() if "Price" in k), None)
        data[estate] = {
            "price_hkd_sqft": _parse_number(price_raw) if price_raw else None,
            "rent_hkd_sqft_month": None,
        }

    for row in _parse_md_table(rent_path):
        estate = row.get("Estate", "").strip()
        if not estate:
            continue
        rent_raw = next((v for k, v in row.items() if "Rent" in k), None)
        rent = _parse_number(rent_raw) if rent_raw else None
        if estate in data:
            data[estate]["rent_hkd_sqft_month"] = rent
        else:
            data[estate] = {"price_hkd_sqft": None, "rent_hkd_sqft_month": rent}

    return data


def load_datasets() -> dict[str, dict[str, dict]]:
    """
    Load all district datasets (cached after the first call).

    Returns
    -------
    {
      "Hong Kong Island": {
        "Taikoo Shing": {"price_hkd_sqft": 17316.0, "rent_hkd_sqft_month": 45.0},
        ...
      },
      "Kowloon": { ... },
      "New Territories": { ... },
    }
    """
    global _datasets_cache
    if _datasets_cache is not None:
        return _datasets_cache
    _datasets_cache = {}
    for district, (pf, rf) in _DISTRICT_FILES.items():
        pp, rp = DATA_DIR / pf, DATA_DIR / rf
        if pp.exists() and rp.exists():
            _datasets_cache[district] = _load_district(pp, rp)
        else:
            _datasets_cache[district] = {}
    if not any(_datasets_cache.values()):
        missing = [f"{pf}, {rf}" for pf, rf in _DISTRICT_FILES.values()]
        raise FileNotFoundError(
            f"No property price datasets found under {DATA_DIR}.\n"
            f"Expected files such as: {', '.join(missing[:2])} …\n"
            "Copy property price tables into config/property_prices/ or set FLOWDRAFT_PROPERTY_DATA."
        )
    return _datasets_cache


def list_districts() -> list[str]:
    """Names of all available districts."""
    return list(_DISTRICT_FILES.keys())


def list_estates(district: str) -> list[str]:
    """Estate names available inside *district*."""
    return list(load_datasets().get(district, {}).keys())


# ─── estimation core ─────────────────────────────────────────────────────────

def _district_averages(district: str) -> tuple[Optional[float], Optional[float]]:
    """Mean price/ft² and rent/ft²/month across all estates in *district*."""
    estates = load_datasets().get(district, {})
    prices = [v["price_hkd_sqft"] for v in estates.values() if v.get("price_hkd_sqft") is not None]
    rents  = [v["rent_hkd_sqft_month"] for v in estates.values() if v.get("rent_hkd_sqft_month") is not None]
    return (
        sum(prices) / len(prices) if prices else None,
        sum(rents)  / len(rents)  if rents  else None,
    )


def estimate_from_area_sqft(
    area_sqft: float,
    district: str,
    estate: Optional[str] = None,
) -> dict:
    """
    Estimate property value and annual ROI for *area_sqft* in *district*.

    Parameters
    ----------
    area_sqft : gross floor area in square feet
    district  : one of list_districts()
    estate    : specific estate name; uses district average when None

    Returns
    -------
    dict with keys:
      area_sqft, district, estate,
      price_per_sqft_hkd, rent_per_sqft_month_hkd,
      property_value_hkd, annual_benefit_hkd, annual_roi_pct
    """
    datasets = load_datasets()
    if district not in datasets:
        raise ValueError(
            f"Unknown district {district!r}. "
            f"Available: {list_districts()}"
        )

    if estate:
        entry = datasets[district].get(estate)
        if entry is None:
            raise ValueError(
                f"Estate {estate!r} not found in {district!r}. "
                f"Use list_estates({district!r}) to see options."
            )
        price_per_sqft = entry["price_hkd_sqft"]
        rent_per_sqft  = entry["rent_hkd_sqft_month"]
        if price_per_sqft is None or rent_per_sqft is None:
            raise ValueError(f"Missing price or rent data for estate {estate!r}.")
    else:
        price_per_sqft, rent_per_sqft = _district_averages(district)
        if price_per_sqft is None or rent_per_sqft is None:
            n = len(datasets.get(district, {}))
            raise ValueError(
                f"No complete price/rent data for district {district!r} "
                f"({n} estates loaded from {DATA_DIR}). "
                "Check that Kowloon.txt and Kowloon_rent.txt exist and are pipe tables."
            )

    property_value  = area_sqft * price_per_sqft
    annual_benefit  = rent_per_sqft * area_sqft * 12
    annual_roi_pct  = annual_benefit / property_value * 100

    return {
        "area_sqft":               round(area_sqft,       2),
        "district":                district,
        "estate":                  estate,
        "price_per_sqft_hkd":      round(price_per_sqft,  2),
        "rent_per_sqft_month_hkd": round(rent_per_sqft,   2),
        "property_value_hkd":      round(property_value,  0),
        "annual_benefit_hkd":      round(annual_benefit,  0),
        "annual_roi_pct":          round(annual_roi_pct,  2),
    }


def estimate_from_graph(
    graph: dict,
    district: str,
    estate: Optional[str] = None,
) -> dict:
    """
    Extract area from a parsed floor-plan graph and return finance estimate.

    The *graph* must be the output of ``parse_with_claude()`` or
    ``parse_floorplan_hybrid()`` (i.e. contain ``graph["meta"]["total_floor_area_m2"]``
    or individual ``space["area_m2"]`` entries).

    The returned dict includes all keys from :func:`estimate_from_area_sqft`
    plus ``area_m2``.
    """
    meta = graph.get("meta") or {}
    area_m2 = meta.get("total_floor_area_m2")
    if area_m2 is None:
        room_areas = [s.get("area_m2") for s in (graph.get("spaces") or []) if s.get("area_m2")]
        if not room_areas:
            raise ValueError(
                "Graph contains no area_m2 data. "
                "Parse the floor plan with diagram_type='FLOORPLAN' first."
            )
        area_m2 = sum(room_areas)

    area_sqft = float(area_m2) * M2_TO_SQFT
    result = estimate_from_area_sqft(area_sqft, district, estate)
    result["area_m2"] = round(float(area_m2), 2)
    return result


# ─── CLI helpers ─────────────────────────────────────────────────────────────

def _print_result(result: dict) -> None:
    sep  = "=" * 52
    thin = "-" * 52
    print(f"\n{sep}")
    print("  Hong Kong Property Finance Estimate")
    print(sep)
    print(f"  District  : {result['district']}")
    if result.get("estate"):
        print(f"  Estate    : {result['estate']}")
    if result.get("area_m2") is not None:
        print(f"  Area      : {result['area_m2']} m2  ({result['area_sqft']} ft2)")
    else:
        print(f"  Area      : {result['area_sqft']} ft2")
    print(f"  Price/ft2 : HKD {result['price_per_sqft_hkd']:>10,.0f}")
    print(f"  Rent/ft2  : HKD {result['rent_per_sqft_month_hkd']:>10.2f} / month")
    print(thin)
    print(f"  Property value  : HKD {result['property_value_hkd']:>14,.0f}")
    print(f"  Annual benefit  : HKD {result['annual_benefit_hkd']:>14,.0f}")
    print(f"  Annual ROI      :     {result['annual_roi_pct']:>13.2f} %")
    print(f"{sep}\n")


# ─── CLI entry point ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="finance",
        description="Hong Kong property value & ROI estimator",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- image subcommand -------------------------------------------------------
    img_p = sub.add_parser(
        "image",
        help="Parse a floor-plan image and estimate property value / ROI",
    )
    img_p.add_argument("image_path", help="Path to floor-plan image")
    img_p.add_argument(
        "--district", required=True, choices=list_districts(),
        help="Target district for price/rent lookup",
    )
    img_p.add_argument(
        "--estate", default=None,
        help="Specific estate (optional; uses district average when omitted)",
    )
    img_p.add_argument(
        "--parser", default="hybrid", choices=["hybrid", "claude"],
        help=(
            "hybrid: Roboflow room detection + Claude labels (default, more accurate); "
            "claude: Claude-only coordinate parser"
        ),
    )
    img_p.add_argument("--model", default="claude-sonnet-4-6")

    # --- area subcommand --------------------------------------------------------
    area_p = sub.add_parser(
        "area",
        help="Estimate value/ROI from a known floor area (m²)",
    )
    area_p.add_argument("area_m2", type=float, help="Floor area in m²")
    area_p.add_argument(
        "--district", required=True, choices=list_districts(),
    )
    area_p.add_argument("--estate", default=None)

    # --- list subcommand --------------------------------------------------------
    lst_p = sub.add_parser(
        "list",
        help="List available districts or estates within a district",
    )
    lst_p.add_argument(
        "--district", default=None, choices=list_districts(),
        help="If given, list estates in that district",
    )

    args = parser.parse_args()

    # ── list ──
    if args.cmd == "list":
        if args.district:
            estates = list_estates(args.district)
            print(f"\nEstates in '{args.district}' ({len(estates)} total):")
            for e in sorted(estates):
                d = load_datasets()[args.district][e]
                p = f"HKD {d['price_hkd_sqft']:,.0f}/ft2" if d.get("price_hkd_sqft") else "-"
                r = f"HKD {d['rent_hkd_sqft_month']:.1f}/ft2/mo" if d.get("rent_hkd_sqft_month") else "-"
                print(f"  {e:<30}  price {p}   rent {r}")
        else:
            print("\nAvailable districts:")
            for d in list_districts():
                n = len(list_estates(d))
                print(f"  {d}  ({n} estates)")
        return

    # ── area ──
    if args.cmd == "area":
        area_sqft = args.area_m2 * M2_TO_SQFT
        result = estimate_from_area_sqft(area_sqft, args.district, args.estate)
        result["area_m2"] = round(args.area_m2, 2)
        _print_result(result)
        return

    # ── image ──
    if args.cmd == "image":
        try:
            from run_utils import load_src
        except ImportError:
            try:
                from src.run_utils import load_src
            except ImportError:
                sys.exit(
                    "ERROR: run_utils not found. "
                    "Run from the repository root or src/ directory."
                )
        try:
            if args.parser == "hybrid":
                parse_mod = load_src("09_floorplan_hybrid.py")
            else:
                parse_mod = load_src("07_claude_parse.py")
        except ModuleNotFoundError as exc:
            sys.exit(
                f"ERROR: missing dependency '{exc.name}'.\n"
                "Install project requirements first:\n"
                "  pip install -r requirements.txt"
            )

        print(f"[1/2] Parsing floor plan: '{args.image_path}' (parser={args.parser}) ...")
        try:
            if args.parser == "hybrid":
                graph = parse_mod.parse_floorplan_hybrid(args.image_path, model=args.model)
            else:
                graph = parse_mod.parse_with_claude(
                    args.image_path, diagram_type="FLOORPLAN", model=args.model
                )
        except Exception as exc:
            sys.exit(f"ERROR: parsing failed — {exc}")

        meta = graph.get("meta") or {}
        area_m2 = meta.get("total_floor_area_m2")
        rooms = len(graph.get("spaces") or [])
        parser_used = meta.get("parser", args.parser)
        print(
            f"      -> parser={parser_used}  rooms={rooms}  "
            f"total_area={area_m2} m2"
        )

        print(f"[2/2] Calculating price & ROI for district='{args.district}' ...")
        result = estimate_from_graph(graph, args.district, args.estate)
        _print_result(result)


if __name__ == "__main__":
    main()
