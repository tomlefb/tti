"""SQLite journal — setups, decisions, outcomes, daily state.

Public surface:

- ``get_engine`` / ``init_db`` / ``session_scope`` — connection management.
- Repository functions: ``insert_setup``, ``get_setup``, ``list_setups``,
  ``insert_decision``, ``get_decision``, ``upsert_outcome``,
  ``get_outcome``, ``get_outcomes_to_match``, ``upsert_daily_state``,
  ``get_daily_state``, ``setup_uid_for``.
- ORM rows: ``SetupRow``, ``DecisionRow``, ``OutcomeRow``, ``DailyStateRow``.

Submodules ``models`` and ``db`` should be imported directly when only
those primitives are needed.
"""

from src.journal.db import get_engine, init_db, session_scope
from src.journal.models import Base, DailyStateRow, DecisionRow, OutcomeRow, SetupRow
from src.journal.repository import (
    get_daily_state,
    get_decision,
    get_outcome,
    get_outcomes_to_match,
    get_setup,
    insert_decision,
    insert_setup,
    list_setups,
    setup_uid_for,
    upsert_daily_state,
    upsert_outcome,
)

__all__ = [
    "Base",
    "DailyStateRow",
    "DecisionRow",
    "OutcomeRow",
    "SetupRow",
    "get_daily_state",
    "get_decision",
    "get_engine",
    "get_outcome",
    "get_outcomes_to_match",
    "get_setup",
    "init_db",
    "insert_decision",
    "insert_setup",
    "list_setups",
    "session_scope",
    "setup_uid_for",
    "upsert_daily_state",
    "upsert_outcome",
]
