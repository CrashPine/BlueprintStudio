"""
engine/validator.py
====================
The compliance engine. Given:
    1. a list of `Rule` objects (loaded from the DB), and
    2. a list of `GeometryObject`s (extracted from the DXF),

it evaluates every rule against the matching geometry and returns a list of
`Violation`s.

Two evaluation paths:
  * Numeric  (min_width / max_width / min_area / min_clearance):
        compare `calculated_metrics[metric]` to the rule's threshold.
  * Topological (must_connect_to / must_exist):
        build an adjacency graph of rooms with networkx (rooms are adjacent if
        their polygons touch / overlap) and check reachability.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from src.compliance_checker.engine.rules import (
    MAX_CONDITIONS,
    NUMERIC_CONDITION_METRIC,
    Condition,
    GeometryObject,
    Rule,
    TargetClass,
)

# How close two room polygons must be to count as "connected" (drawing units).
_ADJACENCY_TOLERANCE = 0.05


@dataclass
class Violation:
    """A single failed rule, tied to the offending geometry (if any)."""
    rule: Rule
    geometry_id: str | None
    message: str
    actual: float | str | None = None

    def __str__(self) -> str:  # pragma: no cover - presentation
        loc = f" [{self.geometry_id}]" if self.geometry_id else ""
        return f"{self.rule.condition} on {self.rule.target_class}" \
               f"/{self.rule.target_type or 'Any'}{loc}: {self.message}"


@dataclass
class ValidationReport:
    """Aggregated result of a validation run."""
    violations: list[Violation] = field(default_factory=list)
    checks_run: int = 0

    @property
    def passed(self) -> bool:
        return not self.violations


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------
def _matches(rule: Rule, obj: GeometryObject) -> bool:
    """Does this geometry object fall under this rule's target?"""
    # `use_enum_values=True` means rule.target_class / obj.cls are plain strings.
    if rule.target_class != TargetClass.ANY.value and rule.target_class != obj.cls:
        return False
    if rule.target_type and rule.target_type not in ("Any", None):
        return (obj.type or "").lower() == str(rule.target_type).lower()
    return True


def _build_adjacency_graph(objects: list[GeometryObject]) -> nx.Graph:
    """
    Build a connectivity graph over ALL objects.

    Edges come from two sources:
      1. Explicit graph links (`obj.links`) — the real MEP/electrical topology
         plus node->space membership from the unified graph. This lets two
         rooms be "connected" when their equipment is wired together.
      2. Polygon adjacency between rooms whose geometries touch / are within
         `_ADJACENCY_TOLERANCE` — a fallback for plans without explicit links
         (e.g. the legacy DXF parser).
    """
    graph = nx.Graph()
    by_id = {o.id: o for o in objects}
    for o in objects:
        graph.add_node(o.id, cls=o.cls, type=o.type)

    # 1. Explicit links.
    for o in objects:
        for other in o.links:
            if other in by_id:
                graph.add_edge(o.id, other)

    # 2. Polygon adjacency between rooms (fallback / reinforcement).
    rooms = [o for o in objects
             if o.cls == TargetClass.ROOM.value and o.geometry is not None]
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            a, b = rooms[i], rooms[j]
            if a.geometry.distance(b.geometry) <= _ADJACENCY_TOLERANCE:
                graph.add_edge(a.id, b.id)
    return graph


# ---------------------------------------------------------------------------
# Per-condition checks
# ---------------------------------------------------------------------------
def _check_numeric(rule: Rule, objects: list[GeometryObject]) -> list[Violation]:
    """Evaluate min/max width/area/clearance rules."""
    metric_key = NUMERIC_CONDITION_METRIC[Condition(rule.condition)]
    threshold = float(rule.value)
    is_min = Condition(rule.condition) not in MAX_CONDITIONS

    violations: list[Violation] = []
    targets = [o for o in objects if _matches(rule, o)]
    for obj in targets:
        actual = obj.calculated_metrics.get(metric_key)
        if actual is None:
            # The metric was not computed for this object (e.g. Building has no
            # clearance, Room has no rated_power_kW). The vocabulary filter should
            # already block such combinations, but if one slips through we skip
            # silently rather than generating a misleading violation.
            continue
        ok = actual >= threshold if is_min else actual <= threshold
        if not ok:
            cmp = ">=" if is_min else "<="
            violations.append(Violation(
                rule, obj.id,
                f"{metric_key}={actual} violates required {metric_key} {cmp} "
                f"{threshold}{rule.unit or ''}.",
                actual=actual,
            ))
    return violations


def _check_must_exist(rule: Rule, objects: list[GeometryObject]) -> list[Violation]:
    """At least one object of the rule's class/type must be present."""
    if any(_matches(rule, o) for o in objects):
        return []
    return [Violation(
        rule, None,
        f"no {rule.target_type or ''} {rule.target_class} found in the plan.",
    )]


def _check_must_connect_to(
    rule: Rule, objects: list[GeometryObject], graph: nx.Graph
) -> list[Violation]:
    """
    Every source entity (matching the rule) must be graph-connected to at least
    one entity of the target type given in rule.value.

    `rule.value` may reference a room category (e.g. 'corridor') OR an equipment
    type (e.g. 'distribution_panel') — we search both so that equipment-to-
    equipment topology rules work correctly.
    """
    target_type = str(rule.value).lower()
    target_ids = {
        o.id for o in objects
        if (o.type or "").lower() == target_type
    }

    if not target_ids:
        return [Violation(rule, None,
                          f"no entity of type '{target_type}' exists to connect to.")]

    violations: list[Violation] = []
    # Sources can be rooms OR equipment depending on the rule's target_class.
    sources = [o for o in objects if _matches(rule, o)
               and o.cls in (TargetClass.ROOM.value, TargetClass.EQUIPMENT.value)]
    for src in sources:
        if src.id not in graph:
            violations.append(Violation(rule, src.id, "not present in adjacency graph."))
            continue
        reachable = nx.node_connected_component(graph, src.id)
        if not (reachable & target_ids):
            violations.append(Violation(
                rule, src.id,
                f"is not connected (directly or transitively) to any '{target_type}'.",
            ))
    return violations


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def validate(rules: list[Rule], objects: list[GeometryObject]) -> ValidationReport:
    """Run all rules against all geometry and return a ValidationReport."""
    report = ValidationReport()
    graph = _build_adjacency_graph(objects)

    numeric_conditions = {c.value for c in NUMERIC_CONDITION_METRIC}
    for rule in rules:
        report.checks_run += 1
        cond = rule.condition  # plain string (use_enum_values=True)

        if cond in numeric_conditions:
            report.violations.extend(_check_numeric(rule, objects))
        elif cond == Condition.MUST_EXIST.value:
            report.violations.extend(_check_must_exist(rule, objects))
        elif cond == Condition.MUST_CONNECT_TO.value:
            report.violations.extend(_check_must_connect_to(rule, objects, graph))
        else:  # pragma: no cover - unknown condition guard
            report.violations.append(Violation(rule, None, f"unsupported condition '{cond}'."))

    return report
