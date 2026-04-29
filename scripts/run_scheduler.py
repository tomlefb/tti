"""Production scheduler launcher (Sprint 6).

Usage on Windows host:

    Ensure MT5 terminal is open and logged in.
    cd C:\\Users\\tomle\\IdeaProjects\\tti
    venv\\Scripts\\activate
    python scripts\\run_scheduler.py

The scheduler runs continuously. Ctrl+C for graceful shutdown.
Logs:       logs/system.log
Journal:    data/journal.db
Dashboard (separate terminal): streamlit run dashboard.py

Hard prohibition (CLAUDE.md rule #1): this script does not place,
modify, or close orders. The operator opens trades manually in MT5.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from _bootstrap import load_settings  # noqa: E402

from src.scheduler.runner import main  # noqa: E402

if __name__ == "__main__":
    settings = load_settings()
    main(settings)
