"""
Deterministic electrical-load math (code computes, LLM never does).
Sums downstream rated_power_kW, checks breaker headroom, benchmarks vs EMSD datacentre EUI.
"""

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# EMSD HK datacentre benchmark stub (illustrative; replace with cached EMSD CSV).
EMSD_DATACENTRE_EUI_KWH_M2_YR = 1800.0  # annual energy use intensity for HK data centres


def _build_adjacency(graph):
    adj = {}
    for edge in graph.get("edges") or []:
        adj.setdefault(edge["from"], []).append(edge["to"])
    return adj


def _node_index(graph):
    return {n["id"]: n for n in graph.get("nodes") or []}


def downstream_load_kW(graph, node_id):
    """Sum rated_power_kW of a node and everything downstream of it (DFS, cycle-safe)."""
    adj = _build_adjacency(graph)
    nodes = _node_index(graph)
    seen = set()

    def _rated(nid):
        attrs = nodes.get(nid, {}).get("attributes") or {}
        return float(attrs.get("rated_power_kW") or attrs.get("power_kW") or 0)

    stack = [node_id]
    total = 0.0
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        total += _rated(nid)
        stack.extend(adj.get(nid, []))
    return round(total, 2)


def breaker_overloaded(graph, node_id, pf=0.9, voltage_V=380, threshold=0.8):
    """3-phase: I = P / (sqrt(3) * V * pf). Overloaded if I > threshold * ampacity."""
    nodes = _node_index(graph)
    node = nodes.get(node_id)
    if not node:
        return {"node_id": node_id, "status": "UNKNOWN", "reason": "node not found"}

    load_kW = downstream_load_kW(graph, node_id)
    attrs = node.get("attributes") or {}
    v = float(attrs.get("voltage_V") or voltage_V)
    amps = (load_kW * 1000.0) / (math.sqrt(3) * v * pf) if v > 0 else 0
    ampacity = attrs.get("ampacity_A")
    if ampacity is None:
        ampacity = attrs.get("current_A")

    result = {
        "node_id": node_id,
        "tag": node.get("tag"),
        "downstream_load_kW": load_kW,
        "computed_amps": round(amps, 1),
        "ampacity_A": ampacity,
        "threshold": threshold,
    }
    if ampacity is None:
        result["status"] = "NO_RATING"
        return result
    result["status"] = "OVERLOAD" if amps > threshold * float(ampacity) else "OK"
    return result


def total_facility_load_kW(graph):
    """Sum every node's rated_power_kW (whole-facility connected load)."""
    total = 0.0
    for n in graph.get("nodes") or []:
        attrs = n.get("attributes") or {}
        total += float(attrs.get("rated_power_kW") or attrs.get("power_kW") or 0)
    return round(total, 2)


def benchmark_vs_emsd(graph):
    """Compare computed annual energy vs EMSD HK datacentre EUI, using parsed space areas."""
    total_area = sum(float(s.get("area_m2") or 0) for s in graph.get("spaces") or [])
    facility_kW = total_facility_load_kW(graph)
    annual_kwh = facility_kW * 8760.0

    result = {
        "facility_load_kW": facility_kW,
        "estimated_annual_kwh": round(annual_kwh, 0),
        "total_area_m2": round(total_area, 1),
        "emsd_benchmark_kwh_m2_yr": EMSD_DATACENTRE_EUI_KWH_M2_YR,
    }
    if total_area > 0:
        actual_eui = annual_kwh / total_area
        result["actual_eui_kwh_m2_yr"] = round(actual_eui, 1)
        result["vs_benchmark_pct"] = round(
            100.0 * actual_eui / EMSD_DATACENTRE_EUI_KWH_M2_YR, 1
        )
        result["verdict"] = (
            "above_benchmark" if actual_eui > EMSD_DATACENTRE_EUI_KWH_M2_YR else "within_benchmark"
        )
    return result


def analyze(graph):
    """One-shot electrical report for the API."""
    breakers = [
        breaker_overloaded(graph, n["id"])
        for n in graph.get("nodes") or []
        if n.get("type") in ("breaker", "distribution_panel", "busway", "transformer")
    ]
    return {
        "total_facility_load_kW": total_facility_load_kW(graph),
        "breakers": breakers,
        "benchmark": benchmark_vs_emsd(graph),
    }
