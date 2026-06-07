"""
Deterministic PUE + BEC checks for datacentre graphs. Code computes — never LLM.
"""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RULES_PATH = ROOT / "config" / "bec_rules.yaml"
if not RULES_PATH.exists():
    RULES_PATH = ROOT / "bec_rules.yaml"  # legacy fallback


def load_rules(path=None):
    if path is None:
        path = RULES_PATH
    if not Path(path).exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def sum_node_power(nodes, types=None):
    total = 0.0
    for n in nodes:
        if types and n.get("type") not in types:
            continue
        attrs = n.get("attributes") or {}
        total += float(attrs.get("rated_power_kW") or attrs.get("power_kW") or 0)
    return total


def compute_pue(graph):
    """
    PUE = facility_power / IT_power
    IT from spaces[].it_power_kW or rack nodes; facility = IT + cooling + electrical overhead.
    """
    meta = graph.get("meta") or {}
    spaces = graph.get("spaces") or []
    nodes = graph.get("nodes") or []

    it_power = meta.get("it_power_kW")
    if it_power is None:
        it_power = sum(float(s.get("it_power_kW") or 0) for s in spaces)
    if it_power <= 0:
        it_power = sum_node_power(nodes, types={"rack"}) or 2000.0

    facility = meta.get("facility_power_kW")
    if facility is None:
        cooling = sum_node_power(
            nodes, types={"chiller", "pump", "cooling_tower", "crac", "crah", "fan"}
        )
        electrical = sum_node_power(
            nodes, types={"ups", "pdu", "transformer", "busway"}
        )
        # overhead: lighting, losses (~15% of IT if not modeled)
        overhead = it_power * 0.15
        facility = it_power + cooling + electrical + overhead

    pue = facility / it_power if it_power > 0 else 999.0
    rules = load_rules()
    target = float(rules.get("pue", {}).get("target_good", meta.get("target_pue", 1.2)))

    return {
        "pue": round(pue, 3),
        "it_power_kW": round(it_power, 1),
        "facility_power_kW": round(facility, 1),
        "target_pue": target,
        "status": "PASS" if pue <= target else "FAIL",
    }


def evaluate_bec(graph):
    """Evaluate BEC rules from bec_rules.yaml against nodes."""
    rules = load_rules()
    nodes = graph.get("nodes") or []
    clauses = []

    for rule in rules.get("equipment_rules") or []:
        applies_to = rule.get("applies_to") or []
        metric = rule.get("metric")
        threshold = rule.get("threshold")
        op = rule.get("op", ">=")
        unit = rule.get("unit", "")

        for n in nodes:
            if n.get("type") not in applies_to:
                continue
            attrs = n.get("attributes") or {}
            actual = None
            if metric == "COP":
                eff = attrs.get("efficiency") or {}
                if eff.get("metric") == "COP":
                    actual = eff.get("value")
            elif metric == "rated_power_kW":
                actual = attrs.get("rated_power_kW")

            if actual is None:
                continue

            passed = _compare(actual, threshold, op)
            clauses.append(
                {
                    "clause": rule.get("clause", "BEC"),
                    "node_id": n["id"],
                    "tag": n.get("tag"),
                    "metric": metric,
                    "actual": actual,
                    "limit": threshold,
                    "unit": unit,
                    "status": "PASS" if passed else "FAIL",
                }
            )

    pue_result = compute_pue(graph)
    pue_rule = rules.get("pue") or {}
    clauses.append(
        {
            "clause": pue_rule.get("clause", "PUE-DC-01"),
            "node_id": None,
            "tag": None,
            "metric": "PUE",
            "actual": pue_result["pue"],
            "limit": pue_result["target_pue"],
            "unit": "ratio",
            "status": pue_result["status"],
        }
    )
    return {"pue": pue_result, "clauses": clauses}


def _compare(actual, threshold, op):
    if op == ">=":
        return actual >= threshold
    if op == "<=":
        return actual <= threshold
    if op == ">":
        return actual > threshold
    if op == "<":
        return actual < threshold
    return actual == threshold


def whatif_pue(graph, node_id, new_attributes):
    """Apply attribute swap and recompute PUE."""
    import copy

    g = copy.deepcopy(graph)
    for n in g.get("nodes") or []:
        if n["id"] == node_id:
            attrs = dict(n.get("attributes") or {})
            attrs.update(new_attributes)
            n["attributes"] = attrs
            break
    before = compute_pue(graph)
    after = compute_pue(g)
    return {
        "before": before,
        "after": after,
        "delta_pue": round(after["pue"] - before["pue"], 4),
        "status": after["status"],
    }
