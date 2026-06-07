"""
engine/rules.py
===============
The Common Data Model that bridges *text* (standards) and *geometry* (DXF).

Two families of Pydantic models live here:

1. `Rule`          - a single, machine-checkable compliance requirement.
                     This is what the LLM extracts from a text standard and
                     what we persist in the database.

2. `GeometryObject` - a single physical entity extracted from the DXF floor
                      plan (a room, an aisle, a rack...) carrying a Shapely
                      geometry and a bag of pre-computed metrics.

Keeping both schemas in one module guarantees the text side and the geometry
side speak *exactly* the same vocabulary (target_class / target_type / metric
names). That shared vocabulary is the whole point of the project.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------
# We use plain str-Enums so the values serialise cleanly to JSON / the DB and
# remain readable in the LLM prompt. The LLM is instructed to use these exact
# tokens; the validator compares against them.

class TargetClass(str, Enum):
    """
    The kind of physical entity a rule (or geometry) refers to.

    Aligned with the ArchDraft x FlowDraft unified graph:
      * ROOM      <- graph `spaces` (target_type carries the category,
                     e.g. 'data_hall', 'electrical_room', 'plant_room').
      * RACK      <- graph `nodes` of type 'rack'.
      * EQUIPMENT <- all other graph `nodes` (target_type = node.type,
                     e.g. 'crac', 'crah', 'ups', 'pdu', 'chiller').
      * AISLE     <- derived from the gaps between parallel rack rows.
      * BUILDING  <- a single synthetic object carrying meta-level metrics
                     (pue, it_power_kW, facility_power_kW).
    """
    ROOM = "Room"
    AISLE = "Aisle"
    RACK = "Rack"
    EQUIPMENT = "Equipment"
    BUILDING = "Building"
    ANY = "Any"


class Condition(str, Enum):
    """
    The supported compliance predicates.

    Numeric conditions compare a `calculated_metrics[...]` value to `value`.
    Topological conditions are checked with networkx in the validator.
    """
    MIN_WIDTH = "min_width"            # metric 'width'  must be >= value
    MAX_WIDTH = "max_width"            # metric 'width'  must be <= value
    MIN_AREA = "min_area"             # metric 'area'   must be >= value
    MIN_CLEARANCE = "min_clearance"     # metric 'clearance' must be >= value
    MIN_POWER = "min_power"          # metric 'rated_power_kW' must be >= value
    MAX_PUE = "max_pue"            # metric 'pue' must be <= value (Building)
    MUST_CONNECT_TO = "must_connect_to"   # entity must be graph-connected to value (a TargetType)
    MUST_EXIST = "must_exist"          # at least one entity of this class/type must be present


# These conditions read a numeric metric off the geometry; the mapping tells
# the validator *which* metric key to inspect for each condition.
NUMERIC_CONDITION_METRIC: dict[Condition, str] = {
    Condition.MIN_WIDTH: "width",
    Condition.MAX_WIDTH: "width",
    Condition.MIN_AREA: "area",
    Condition.MIN_CLEARANCE: "clearance",
    Condition.MIN_POWER: "rated_power_kW",
    Condition.MAX_PUE: "pue",
}

# Conditions whose check is "actual <= value" (everything else is "actual >= value").
MAX_CONDITIONS: set = {Condition.MAX_WIDTH, Condition.MAX_PUE}


# ---------------------------------------------------------------------------
# Controlled target_type vocabulary (mirrors the unified-graph schema)
# ---------------------------------------------------------------------------
# A local LLM (mistral) follows instructions loosely and invents target_types.
# These sets are the SINGLE source of truth for which (class, type) pairs are
# checkable against the graph; `is_rule_in_vocabulary` uses them to drop noise.
ROOM_CATEGORIES: set[str] = {
    "data_hall", "electrical_room", "plant_room", "office", "corridor", "unknown",
}
EQUIPMENT_TYPES: set[str] = {
    "chiller", "pump", "cooling_tower", "ahu", "fcu", "boiler", "fan",
    "transformer", "switchgear", "breaker", "distribution_panel", "meter",
    "valve", "sensor", "instrument", "fitting", "crac", "crah", "ups",
    "pdu", "busway", "rack",
}
AISLE_TYPES: set[str] = {"cold", "hot"}
# Every token a topological rule (must_connect_to / must_exist) may reference.
_ALL_TOKENS: set[str] = ROOM_CATEGORIES | EQUIPMENT_TYPES | AISLE_TYPES


def is_rule_in_vocabulary(rule: "Rule") -> bool:
    """
    True if a Rule references only known, graph-checkable entities.

    Filters out hallucinated/off-vocabulary rules a local model may emit
    (e.g. target_class 'Building' with `must_exist` "Sustainable Urbanism Bonus",
    or an Equipment type that does not exist in the graph schema).
    """
    tc = rule.target_class                      # plain str (use_enum_values=True)
    tt = (rule.target_type or "").strip().lower()
    cond = rule.condition

    if tc not in {"Room", "Aisle", "Rack", "Equipment", "Building", "Any"}:
        return False

    # target_type must fit the class.
    if tc == "Room" and tt and tt not in ROOM_CATEGORIES:
        return False
    if tc == "Equipment" and tt not in EQUIPMENT_TYPES:
        return False
    if tc == "Aisle" and tt not in AISLE_TYPES:
        return False
    if tc == "Building" and tt not in ("", "building"):
        return False

    # Each condition is only meaningful on certain target classes.
    # e.g. min_power on a Room, or min_clearance on a Building, is nonsensical.
    _COND_VALID_CLASSES: dict[str, set[str]] = {
        "min_area":      {"Room"},
        "min_width":     {"Room", "Aisle", "Any"},
        "max_width":     {"Room", "Aisle", "Any"},
        "min_clearance": {"Room", "Aisle", "Any"},
        "min_power":     {"Equipment", "Rack"},
        "max_pue":       {"Building"},
    }
    if cond in _COND_VALID_CLASSES and tc not in _COND_VALID_CLASSES[cond]:
        return False

    # Topological condition sanity.
    if cond == "must_exist":
        if tc not in ("Room", "Equipment", "Rack", "Aisle"):
            return False
        if not tt and tc != "Rack":
            return False
    if cond == "must_connect_to":
        if str(rule.value).strip().lower() not in _ALL_TOKENS:
            return False

    return True


# ---------------------------------------------------------------------------
# Rule model (text -> DB)
# ---------------------------------------------------------------------------
class Rule(BaseModel):
    """
    A single compliance rule.

    Example (as JSON, which is what the LLM returns):
        {
            "target_class": "Aisle",
            "target_type": "Cold",
            "condition": "min_width",
            "value": 1.2,
            "unit": "m",
            "description": "Cold aisles must be at least 1.2 m wide."
        }
    """
    model_config = ConfigDict(use_enum_values=True)

    target_class: TargetClass = Field(
        ..., description="Entity class the rule applies to (Room, Aisle, Rack, Any)."
    )
    target_type: Optional[str] = Field(
        None,
        description="Sub-type, e.g. 'Cold', 'Hot', 'ER', 'MDA'. None/'Any' means all types.",
    )
    condition: Condition = Field(..., description="The predicate to evaluate.")
    value: Union[float, str] = Field(
        ...,
        description="Numeric threshold (for min/max conditions) or a string "
        "target_type (for topological conditions like must_connect_to).",
    )
    unit: Optional[str] = Field(None, description="Unit of the value, e.g. 'm'.")
    description: Optional[str] = Field(
        None, description="Human-readable restatement of the rule."
    )
    source: Optional[str] = Field(
        None, description="Citation, e.g. 'TIA-942 §5.3'."
    )

    @field_validator("value")
    @classmethod
    def _coerce_value(cls, v: Any) -> Union[float, str]:
        """Accept ints/strings; keep numeric values as floats where possible."""
        if isinstance(v, (int, float)):
            return float(v)
        # Topological values stay as strings (e.g. "MDA").
        s = str(v).strip()
        try:
            return float(s)
        except ValueError:
            return s

    @property
    def is_numeric(self) -> bool:
        return self.condition in NUMERIC_CONDITION_METRIC

    def normalize_units(self) -> None:
        """Convert known units to standard base units (m, m2, kW) in-place."""
        if not self.is_numeric or not isinstance(self.value, (int, float)):
            return

        u = (self.unit or "").strip().lower()
        v = float(self.value)

        # Length -> meters
        if self.condition in (Condition.MIN_WIDTH, Condition.MAX_WIDTH, Condition.MIN_CLEARANCE):
            if u in ("mm", "millimeters", "millimetres"):
                v = v / 1000.0
                u = "m"
            elif u in ("cm", "centimeters", "centimetres"):
                v = v / 100.0
                u = "m"
            elif u in ("ft", "feet", "'"):
                v = v * 0.3048
                u = "m"
            elif u in ("in", "inches", '"'):
                v = v * 0.0254
                u = "m"
            elif not u:
                u = "m"  # Default assumption
            self.value = v
            self.unit = u

        # Area -> square meters
        elif self.condition == Condition.MIN_AREA:
            if u in ("sqft", "sq ft", "ft2", "sq. ft."):
                v = v * 0.092903
                u = "m2"
            elif not u:
                u = "m2"
            self.value = v
            self.unit = u

        # Power -> kilowatts
        elif self.condition == Condition.MIN_POWER:
            if u in ("w", "watts"):
                v = v / 1000.0
                u = "kW"
            elif u in ("mw", "megawatts"):
                v = v * 1000.0
                u = "kW"
            elif not u:
                u = "kW"
            self.value = v
            self.unit = u


class RuleSet(BaseModel):
    """Wrapper so the LLM can return a single top-level JSON object."""
    rules: list[Rule] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Geometry model (DXF -> validator)
# ---------------------------------------------------------------------------
class GeometryObject(BaseModel):
    """
    A standardized physical entity extracted from the DXF.

    `geometry` is a Shapely object (Polygon / LineString). We allow arbitrary
    types so Pydantic does not try to validate/serialise the Shapely instance.

    `populate_by_name` lets us build it either as GeometryObject(cls=...) or
    GeometryObject(**{'class': ...}), since 'class' is a reserved Python word.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    id: str = Field(..., description="Unique identifier, e.g. 'aisle-01'.")
    cls: TargetClass = Field(
        ..., alias="class", description="Entity class (Room, Aisle, Rack)."
    )
    type: Optional[str] = Field(
        None, description="Sub-type, e.g. 'Cold', 'MDA', 'ER'."
    )
    geometry: Any = Field(
        None, description="Shapely geometry (Polygon/LineString) or None."
    )
    calculated_metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Pre-computed metrics, e.g. {'width': 1.2, 'area': 15.5}.",
    )
    links: list[str] = Field(
        default_factory=list,
        description="IDs of other objects this one is connected to "
        "(graph edges + space membership). Used for topology checks.",
    )
