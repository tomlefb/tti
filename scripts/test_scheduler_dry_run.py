"""Mac-friendly dry run of the Sprint 6 scheduler (no MT5, no Telegram).

What it does:

1. Loads ``config/settings.py`` (or ``settings.py.example`` as fallback,
   so the dev Mac can run without secrets).
2. Builds a fake MT5 client that serves historical fixture parquet
   frames for the four watched pairs (XAUUSD/NDX100/EURUSD/GBPUSD).
3. Builds a no-network ``TelegramNotifier`` whose async methods just
   record what would have been sent.
4. Calls ``run_detection_cycle`` ONCE synchronously, anchored to a known
   A-grade fixture date (XAUUSD 2026-01-02 NY by default).
5. Prints the resulting :class:`CycleReport` plus a summary of the
   captured notifications.

Use this on the dev Mac to validate the wiring before deploying to
the Windows host.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_settings() -> ModuleType:
    """Mirror the strategy used by ``scripts/test_notification.py``."""
    settings_real = _REPO_ROOT / "config" / "settings.py"
    settings_example = _REPO_ROOT / "config" / "settings.py.example"

    if settings_real.exists():
        from _bootstrap import load_settings as _bootstrap_load

        return _bootstrap_load()

    if not settings_example.exists():
        print("ERROR: config/settings.py.example missing.", file=sys.stderr)
        raise SystemExit(2)

    if "config.secrets" not in sys.modules:
        secrets_stub = ModuleType("config.secrets")
        for name in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "MT5_LOGIN",
            "MT5_PASSWORD",
            "MT5_SERVER",
        ):
            setattr(secrets_stub, name, None)
        sys.modules["config.secrets"] = secrets_stub

    loader = SourceFileLoader("config.settings", str(settings_example))
    module = ModuleType(loader.name)
    module.__file__ = str(settings_example)
    sys.modules["config.settings"] = module
    loader.exec_module(module)

    # Override MT5_LOGIN to a usable int, and MAX_LOSS_OVERRIDE etc — the
    # fallback path mostly works because settings.py.example carries everything.
    if module.MT5_LOGIN is None:
        module.MT5_LOGIN = 0
    if not hasattr(module, "MAX_LOSS_OVERRIDE"):
        module.MAX_LOSS_OVERRIDE = False
    return module


_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"


class _FixtureMt5Client:
    """Fake MT5 client serving committed parquet OHLC fixtures.

    ``fetch_ohlc`` ignores ``n_candles`` and returns the entire fixture
    frame — sufficient for a single-cycle dry run.
    """

    def __init__(self, target_date: date):
        self._target_date = target_date
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    def fetch_ohlc(self, symbol: str, timeframe: str, n_candles: int) -> pd.DataFrame:
        key = (symbol, timeframe)
        if key not in self._cache:
            path = _FIXTURE_DIR / f"{symbol}_{timeframe}.parquet"
            if not path.exists():
                raise FileNotFoundError(f"missing fixture {path}")
            df = pd.read_parquet(path)
            df["time"] = pd.to_datetime(df["time"], utc=True)
            self._cache[key] = df
        return self._cache[key]

    def get_account_info(self):
        return SimpleNamespace(
            login_masked="***0000",
            currency="USD",
            balance=5000.0,
            equity=5000.0,
            profit=0.0,
            margin_level=0.0,
            leverage=100,
        )

    def get_recent_trades(self, since):
        return []


def _make_recording_notifier():
    """Capture send_setup / send_text / send_error calls without network."""
    captured: dict[str, list] = {"setup": [], "text": [], "error": []}

    n = MagicMock()

    async def _send_setup(setup, chart_path, **kwargs):
        captured["setup"].append((setup.symbol, setup.quality, str(chart_path)))
        return True

    async def _send_text(text, **kwargs):
        captured["text"].append(text)
        return True

    async def _send_error(text):
        captured["error"].append(text)
        return True

    n.send_setup = _send_setup
    n.send_text = _send_text
    n.send_error = _send_error
    return n, captured


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--date",
        default="2026-01-02",
        help="Trading date used to anchor the dry-run cycle (default 2026-01-02).",
    )
    parser.add_argument(
        "--killzone",
        choices=("london", "ny"),
        default="ny",
        help="Killzone to simulate (default ny — picks 14:00 UTC).",
    )
    args = parser.parse_args()

    settings = _load_settings()
    target_date = date.fromisoformat(args.date)

    # Pick a UTC time inside the requested killzone.
    paris_tz = ZoneInfo("Europe/Paris")
    kz_session = settings.KILLZONE_NY if args.killzone == "ny" else settings.KILLZONE_LONDON
    kz_start_paris = datetime.combine(
        target_date, time(kz_session[0], kz_session[1]), tzinfo=paris_tz
    )
    # Anchor 30 min into the killzone so killzone gating accepts MSS confirms.
    now_paris = kz_start_paris + pd.Timedelta(minutes=30).to_pytimedelta()
    now_utc = now_paris.astimezone(UTC)

    print("=== TJR scheduler dry-run ===")
    print(f"target_date = {target_date} | killzone = {args.killzone}")
    print(f"now_utc     = {now_utc.isoformat()}")
    print(f"watched     = {settings.WATCHED_PAIRS}")
    print()

    mt5 = _FixtureMt5Client(target_date)
    notifier, captured = _make_recording_notifier()

    # In-memory journal — never touch the operator's real DB.
    from src.journal.db import get_engine, init_db, session_scope
    from src.scheduler.jobs import run_detection_cycle

    engine = get_engine(":memory:")
    init_db(engine)

    def session_factory():
        return session_scope(engine)

    chart_render_log: list = []

    def chart_send_callback(setup, chart_path):
        chart_render_log.append((setup.symbol, setup.quality, setup.killzone, chart_path))

    report = run_detection_cycle(
        mt5,
        session_factory,
        notifier,
        settings,
        now_utc=now_utc,
        chart_send_callback=chart_send_callback,
    )

    # Persisted-row count from the in-memory journal.
    from src.journal.models import SetupRow

    with session_factory() as s:
        rows = s.query(SetupRow).all()
        notified = sum(1 for r in rows if r.was_notified)
        rejected = sum(1 for r in rows if not r.was_notified)
        rejection_breakdown: dict[str, int] = {}
        for r in rows:
            if not r.was_notified:
                rejection_breakdown[r.rejection_reason or "?"] = (
                    rejection_breakdown.get(r.rejection_reason or "?", 0) + 1
                )

    print("---- CycleReport ----")
    print(f"pairs_processed   = {report.pairs_processed}")
    print(f"setups_detected   = {report.setups_detected}")
    print(f"setups_notified   = {report.setups_notified}")
    print(f"setups_rejected   = {report.setups_rejected}")
    print(f"blocks            = {report.blocks}")
    print(f"errors            = {report.errors}")
    print()
    print(f"journal_notified  = {notified}")
    print(f"journal_rejected  = {rejected}")
    if rejection_breakdown:
        print(f"  by reason: {rejection_breakdown}")
    print()
    print(f"chart_send_calls  = {len(chart_render_log)}")
    for sym, q, kz, _path in chart_render_log:
        print(f"  - {sym} {q} ({kz})")
    print()
    print("---- Telegram capture (no network) ----")
    print(
        f"send_setup x{len(captured['setup'])} | send_text x{len(captured['text'])} "
        f"| send_error x{len(captured['error'])}"
    )
    if captured["setup"]:
        print("  send_setup payloads:")
        for sym, q, _path in captured["setup"]:
            print(f"    - {sym} {q}")
    print()
    print("Dry run complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
