"""
database/models.py
==================
SQLAlchemy ORM models for persisting compliance rules.

The `RuleORM` table mirrors the `Rule` Pydantic schema in engine/rules.py.
Helper methods convert between the two representations so the rest of the app
only ever deals with Pydantic `Rule` objects.
"""

from __future__ import annotations

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.compliance_checker.engine.rules import Condition, Rule, TargetClass


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


class RuleORM(Base):
    """
    A compliance rule as stored in PostgreSQL.

    `value` is stored in two columns (numeric + string) because a rule's value
    is polymorphic: numeric for thresholds (min_width=1.2) and textual for
    topological targets (must_connect_to='MDA'). Exactly one is populated.
    """
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_class: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    condition: Mapped[str] = mapped_column(String(32), nullable=False)

    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_str: Mapped[str | None] = mapped_column(String(128), nullable=True)

    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ------------------------------------------------------------------
    # Pydantic <-> ORM conversion
    # ------------------------------------------------------------------
    @classmethod
    def from_pydantic(cls, rule: Rule) -> "RuleORM":
        """Build an ORM row from a Pydantic Rule."""
        value_num: float | None = None
        value_str: str | None = None
        if isinstance(rule.value, (int, float)):
            value_num = float(rule.value)
        else:
            value_str = str(rule.value)

        return cls(
            target_class=str(rule.target_class),
            target_type=rule.target_type,
            condition=str(rule.condition),
            value_num=value_num,
            value_str=value_str,
            unit=rule.unit,
            description=rule.description,
            source=rule.source,
        )

    def to_pydantic(self) -> Rule:
        """Reconstruct a Pydantic Rule from this ORM row."""
        value: float | str = self.value_num if self.value_num is not None else self.value_str
        return Rule(
            target_class=TargetClass(self.target_class),
            target_type=self.target_type,
            condition=Condition(self.condition),
            value=value,
            unit=self.unit,
            description=self.description,
            source=self.source,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<RuleORM {self.target_class}/{self.target_type} "
            f"{self.condition}={self.value_num or self.value_str}>"
        )
