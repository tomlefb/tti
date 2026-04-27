"""Shared bootstrap for smoke-test scripts.

- Adds the project root to ``sys.path`` so ``config`` and ``src`` import
  cleanly when the script is invoked as ``python scripts/test_xxx.py``.
- Loads ``config.settings`` and surfaces a clear, actionable error if the
  user has not yet copied the ``.example`` templates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_repo_on_path() -> None:
    repo_str = str(_REPO_ROOT)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)


def load_settings() -> ModuleType:
    """Import and return ``config.settings`` with a friendly error on failure.

    Returns:
        The imported settings module.

    Raises:
        SystemExit: when ``config/settings.py`` or ``config/secrets.py`` is
            missing — prints a clear message pointing at the templates.
    """
    _ensure_repo_on_path()

    settings_path = _REPO_ROOT / "config" / "settings.py"
    secrets_path = _REPO_ROOT / "config" / "secrets.py"

    missing: list[str] = []
    if not settings_path.exists():
        missing.append("config/settings.py (copy from config/settings.py.example)")
    if not secrets_path.exists():
        missing.append("config/secrets.py (copy from config/secrets.py.example)")

    if missing:
        msg = (
            "Configuration files are missing. Please create them before "
            "running this script:\n  - " + "\n  - ".join(missing)
        )
        print(f"ERROR: {msg}", file=sys.stderr)
        raise SystemExit(2)

    try:
        from config import settings  # noqa: WPS433 (intentional local import)
    except ImportError as exc:  # pragma: no cover — surfaced to operator
        print(
            "ERROR: failed to import config.settings — check the file for "
            f"syntax errors or unfilled secrets.\n  cause: {exc!r}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    return settings
