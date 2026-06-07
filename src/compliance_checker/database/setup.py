"""
database/setup.py
=================
Database initialization and a couple of small CRUD helpers.

For the proof-of-concept we try the configured PostgreSQL `DATABASE_URL` first;
if the connection fails (no Postgres on the dev machine) we transparently fall
back to an in-memory SQLite database so the whole pipeline still runs end-to-end.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from src.compliance_checker.database.models import Base, RuleORM
from src.compliance_checker.engine.rules import Rule

# Persistent SQLite fallback lives next to the compliance sample data so rules
# survive across separate API requests (in-memory SQLite would be recreated
# empty on every call, losing the saved ruleset before validation runs).
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_SQLITE_PATH = os.path.join(_DATA_DIR, "dc_compliance.db")

# Cache the session factory so every request in a process shares one engine.
_SESSION_FACTORY: sessionmaker | None = None


def _make_engine() -> Engine:
    """
    Create the SQLAlchemy engine.

    Tries the Postgres URL from the environment; on any connection error it
    falls back to a file-based SQLite database so the saved ruleset persists
    across requests (useful for demos/CI without Postgres).
    """
    url = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://dc_user:dc_password@localhost:5432/dc_compliance",
    )
    try:
        engine = create_engine(url, future=True)
        # Force a real connection so we can detect failure now, not later.
        with engine.connect():
            pass
        print(f"[db] Connected to PostgreSQL: {url.split('@')[-1]}")
        return engine
    except (OperationalError, SQLAlchemyError, Exception) as exc:  # noqa: BLE001
        print(f"[db] Postgres unavailable ({exc.__class__.__name__}); "
              f"falling back to file SQLite at {_SQLITE_PATH}.")
        os.makedirs(_DATA_DIR, exist_ok=True)
        return create_engine(
            f"sqlite:///{_SQLITE_PATH}",
            future=True,
            connect_args={"check_same_thread": False},
        )


def init_db() -> sessionmaker:
    """
    Initialise the database, create tables and return a cached session factory.
    """
    global _SESSION_FACTORY
    if _SESSION_FACTORY is not None:
        return _SESSION_FACTORY
    engine = _make_engine()
    Base.metadata.create_all(engine)
    _SESSION_FACTORY = sessionmaker(bind=engine, future=True)
    return _SESSION_FACTORY


def save_rules(session_factory: sessionmaker, rules: list[Rule], replace: bool = True) -> int:
    """
    Persist a list of Pydantic Rules. Returns the number stored.

    By default this replaces the stored ruleset (clears existing rows first) so
    re-uploading a standard does not accumulate duplicates.
    """
    with session_factory() as session:  # type: Session
        if replace:
            session.query(RuleORM).delete()
        session.add_all(RuleORM.from_pydantic(r) for r in rules)
        session.commit()
    return len(rules)


def load_rules(session_factory: sessionmaker) -> list[Rule]:
    """Load all rules back out as Pydantic Rule objects."""
    with session_factory() as session:  # type: Session
        rows = session.query(RuleORM).all()
        return [row.to_pydantic() for row in rows]
