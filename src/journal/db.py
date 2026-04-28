"""Engine + session factory for the SQLite journal.

Public entry points:

- ``get_engine(db_path)`` — build a SQLAlchemy ``Engine`` configured with
  the ``foreign_keys=ON`` SQLite pragma and a thread-safe connection.
- ``init_db(engine)`` — create all tables if they don't exist
  (programmatic migration; Sprint 5 schema is small enough to skip
  Alembic).
- ``session_scope(engine)`` — context manager yielding a transactional
  ``Session``; commits on clean exit, rolls back on exception.

In-memory testing: pass ``":memory:"`` (with the ``sqlite:///:memory:``
URL handled internally) to get a throwaway DB; tests in
``tests/journal/`` use this pattern.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.journal.models import Base


def get_engine(db_path: str | Path) -> Engine:
    """Create a SQLAlchemy engine for the journal SQLite file.

    Args:
        db_path: filesystem path or the literal ``":memory:"`` string for
            an in-memory database (handy in tests).

    The returned engine has ``check_same_thread=False`` so an async
    Telegram callback running on the bot's thread can safely write — the
    SQLAlchemy session itself remains the unit of single-threaded access.
    Foreign-key enforcement is wired via a ``connect`` event so every
    new connection runs ``PRAGMA foreign_keys=ON``.
    """
    if isinstance(db_path, Path):
        url = f"sqlite:///{db_path}"
    elif db_path == ":memory:":
        url = "sqlite:///:memory:"
    else:
        url = f"sqlite:///{db_path}"

    engine = create_engine(
        url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create every journal table on ``engine`` if not already present."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine) -> Generator[Session, None, None]:
    """Transactional session — commit on clean exit, rollback on raise."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
