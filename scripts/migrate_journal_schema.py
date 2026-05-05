"""Idempotent journal schema migration.

Brings ``data/journal.db`` (or any path passed via ``--db``) up to the
current ``src/journal/models.py`` shape by adding columns the table is
missing. **Non-destructive**: never drops, never alters existing data,
never changes existing column types. Re-running is a no-op.

Why this exists
---------------
Sprint 5 created the journal schema; subsequent sprints added columns
to existing tables (e.g. Sprint 6.5 added the ETHUSD bias columns,
Sprint 7 added ``auto_trading_disabled`` / ``disabled_reason``).
SQLAlchemy's ``Base.metadata.create_all`` only creates **missing
tables** — it does NOT add columns to existing tables. As a result,
journals that started life on an older sprint accumulate schema
drift versus the model definitions, and SQL queries that touch the
new columns raise ``OperationalError: no such column: …``.

The stage-3 rotation smoke test surfaced this exact failure on the
operator's host. From here on, **every schema change to an existing
table requires an explicit migration entry below** (see protocol
§04 — "Schema migration policy").

Run
---
    python -m scripts.migrate_journal_schema [--db PATH] [--dry-run]

The default DB path is ``data/journal.db`` (matches the runtime
``DB_PATH`` setting). ``--dry-run`` reports what would be added
without touching the file.

Each migration is recorded inline as a ``(table, column, sql_type,
default_clause_or_None)`` tuple. The script reads the live schema
via ``PRAGMA table_info`` and only emits ``ALTER TABLE ADD COLUMN``
for columns the table doesn't already have.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Migration manifest
# ---------------------------------------------------------------------------
# (table, column, sql_type, default_clause)
# default_clause is a SQL fragment INCLUDING the keyword (e.g. "NOT NULL
# DEFAULT 0"). Use None when the column is plain nullable with no default.
#
# Add new entries at the END of the list with a comment naming the sprint
# that introduced them. Never remove or reorder existing entries — they
# describe the migration history, not just the desired end state.
_MIGRATIONS: list[tuple[str, str, str, str | None]] = [
    # ---- Sprint 6.5: ETHUSD bias caching (later dropped from WATCHED_PAIRS
    # at Sprint 6.6 but the columns stay for backward-compat reads). ----
    ("daily_state", "bias_ethusd_london", "VARCHAR", None),
    ("daily_state", "bias_ethusd_ny", "VARCHAR", None),
    # ---- Sprint 7: auto-execution day-disabled flag. ----
    ("daily_state", "auto_trading_disabled", "BOOLEAN", "NOT NULL DEFAULT 0"),
    ("daily_state", "disabled_reason", "VARCHAR", None),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _existing_columns(con: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names the table currently has."""
    cur = con.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _alter_add_column(
    con: sqlite3.Connection,
    *,
    table: str,
    column: str,
    sql_type: str,
    default_clause: str | None,
) -> None:
    """Issue an ``ALTER TABLE ADD COLUMN``. Caller must verify the column
    is missing first — ``ALTER TABLE ADD`` is NOT idempotent in SQLite."""
    parts = [f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"]
    if default_clause:
        parts.append(default_clause)
    sql = " ".join(parts)
    logger.info("running: %s", sql)
    con.execute(sql)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def migrate(db_path: Path, *, dry_run: bool = False) -> dict:
    """Apply the migration manifest to ``db_path``. Returns a summary dict.

    Idempotent: columns already present are reported as "skipped"; only
    missing ones get ``ALTER TABLE ADD COLUMN``.
    """
    if not db_path.exists():
        raise FileNotFoundError(
            f"journal DB not found at {db_path}. "
            f"If this is a fresh install, run the scheduler once "
            f"(or call ``init_db``) to create it before migrating."
        )

    summary = {"added": [], "skipped": [], "missing_table": []}
    con = sqlite3.connect(str(db_path))
    try:
        for table, column, sql_type, default_clause in _MIGRATIONS:
            if not _table_exists(con, table):
                logger.warning(
                    "table %r missing — leaving for ``init_db`` to create",
                    table,
                )
                summary["missing_table"].append((table, column))
                continue
            existing = _existing_columns(con, table)
            if column in existing:
                logger.info("skip: %s.%s already present", table, column)
                summary["skipped"].append((table, column))
                continue
            if dry_run:
                logger.info("DRY-RUN: would add %s.%s %s%s",
                            table, column, sql_type,
                            f" {default_clause}" if default_clause else "")
                summary["added"].append((table, column))
                continue
            _alter_add_column(
                con, table=table, column=column,
                sql_type=sql_type, default_clause=default_clause,
            )
            summary["added"].append((table, column))
        if not dry_run:
            con.commit()
    finally:
        con.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--db",
        type=Path,
        default=_REPO_ROOT / "data" / "journal.db",
        help="Path to the journal SQLite file (default: data/journal.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be added without modifying the DB.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    summary = migrate(args.db, dry_run=args.dry_run)
    print()
    print("=" * 60)
    print(f"DB: {args.db}")
    print(f"Mode: {'dry-run' if args.dry_run else 'applied'}")
    print(f"Added:    {len(summary['added'])} column(s)")
    for tbl, col in summary["added"]:
        print(f"  + {tbl}.{col}")
    print(f"Skipped:  {len(summary['skipped'])} column(s) already present")
    for tbl, col in summary["skipped"]:
        print(f"  = {tbl}.{col}")
    if summary["missing_table"]:
        print(f"Missing tables: {len(summary['missing_table'])}")
        for tbl, col in summary["missing_table"]:
            print(f"  ! {tbl} (would-be column {col!r})")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
