"""CLI: reconcile MT5 trade history with journaled ``Taken`` setups.

Usage:
    python scripts/run_outcome_tracker.py [--since-days N]

Sprint 5 ships this as a manual tool. The Sprint 6 scheduler will
invoke ``reconcile_outcomes`` directly as a daily cron at 23:00 Paris.

This script does NOT auto-trigger anywhere — running it is always an
explicit operator action.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_settings() -> ModuleType:
    """Load ``config.settings`` (real one only — outcome tracker needs MT5)."""
    settings_real = _REPO_ROOT / "config" / "settings.py"
    if not settings_real.exists():
        print(
            "ERROR: config/settings.py is missing — fill in real secrets to "
            "run the outcome tracker.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    loader = SourceFileLoader("config.settings", str(settings_real))
    module = ModuleType(loader.name)
    module.__file__ = str(settings_real)
    sys.modules.setdefault("config.settings", module)
    loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--since-days",
        type=int,
        default=7,
        help="Reconcile trades closed within the last N days (default: 7).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    settings = _load_settings()

    # Late imports — heavy SQLAlchemy / MT5 stack only after CLI parsing.
    from src.journal.db import get_engine, init_db, session_scope
    from src.journal.outcome_tracker import (
        DEFAULT_MATCH_WINDOW_MINUTES,
        reconcile_outcomes,
    )

    try:
        from src import mt5_client  # noqa: F401
    except ImportError as exc:
        print(f"ERROR: cannot import mt5_client: {exc!r}", file=sys.stderr)
        return 1

    # mt5_client is a stub until Sprint 6 (per docs/03 roadmap). Bail out
    # cleanly so the operator gets a useful message rather than a stack trace.
    if not hasattr(mt5_client, "Client") and not hasattr(mt5_client, "get_recent_trades"):
        print(
            "ERROR: mt5_client wrapper is not implemented yet (planned in "
            "Sprint 6). The outcome tracker can be exercised today via its "
            "unit tests with a mock client; production reconciliation will "
            "wire up once mt5_client.Client lands.",
            file=sys.stderr,
        )
        return 1

    engine = get_engine(settings.DB_PATH)
    init_db(engine)

    since = datetime.now(tz=UTC) - timedelta(days=args.since_days)
    match_window = int(
        getattr(settings, "OUTCOME_MATCH_WINDOW_MINUTES", DEFAULT_MATCH_WINDOW_MINUTES)
    )

    # ``Client`` is the canonical name; fall back to a module-level
    # ``get_recent_trades`` if the wrapper exposes a function-style API.
    client = mt5_client.Client() if hasattr(mt5_client, "Client") else mt5_client  # type: ignore[attr-defined]

    with session_scope(engine) as s:
        upserted = reconcile_outcomes(
            s,
            client,
            since=since,
            match_window_minutes=match_window,
        )

    if upserted == 0:
        print("No outcomes upserted (no pending setups or no matching trades).")
    else:
        print(f"Reconciled {upserted} outcome row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
