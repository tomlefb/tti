"""Logging bootstrap for the TJR system.

Configures stdlib logging per `docs/04_PROJECT_RULES.md` (Logging section):

- Root logger level controlled by `config.settings.LOG_LEVEL`.
- Rotating file handler: 10 MB × 5 files at `logs/system.log` (configurable
  through `LOG_FILE`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`).
- Console handler at INFO level.
- Per-module loggers obtained via `logging.getLogger(__name__)` continue to
  inherit from the root once `setup_logging()` is called.

`setup_logging()` is intentionally NOT called automatically at import time.
The scheduler entrypoint and the smoke-test scripts call it explicitly.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(
    *,
    log_file: str | Path | None = None,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    level: str | int | None = None,
    console_level: str | int = logging.INFO,
) -> logging.Logger:
    """Configure root logging for the process.

    All values default to those in `config.settings` so callers normally
    invoke this with no arguments. Arguments exist for tests and ad hoc
    scripts that want to override without touching the config module.

    Args:
        log_file: Path to the rotating log file. Defaults to
            `config.settings.LOG_FILE`.
        max_bytes: Per-file size limit in bytes before rotation. Defaults
            to `config.settings.LOG_MAX_BYTES`.
        backup_count: Number of rotated files to retain. Defaults to
            `config.settings.LOG_BACKUP_COUNT`.
        level: Root logger level (str or int). Defaults to
            `config.settings.LOG_LEVEL`.
        console_level: Level for the console handler. Defaults to INFO.

    Returns:
        The configured root logger, for convenience.
    """
    # Local import so this module remains importable even before secrets.py
    # exists (useful in CI / linting contexts). The smoke-test scripts that
    # actually use logging at runtime require a populated config anyway.
    from config import settings  # noqa: WPS433 (intentional local import)

    file_path = Path(log_file or settings.LOG_FILE)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level or settings.LOG_LEVEL)

    # Reset any handlers attached by a previous call (e.g. in tests).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(fmt=_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(file_path),
        maxBytes=int(max_bytes or settings.LOG_MAX_BYTES),
        backupCount=int(backup_count or settings.LOG_BACKUP_COUNT),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    return root
