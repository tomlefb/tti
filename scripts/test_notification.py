"""End-to-end notification smoke test (Sprint 4).

The operator runs this on the Windows host (or any machine with internet
+ python-telegram-bot installed) to validate the full notification flow:

    1. Build a synthetic Setup using a real fixture from a known A-grade
       day (XAUUSD 2026-01-02 NY short by default).
    2. Render the chart to ``runtime_charts/`` via ``chart_renderer``.
    3. Print the formatted Telegram caption to stdout.
    4. Send the chart PNG + caption + Taken/Skipped buttons to Telegram.
    5. Poll callbacks for ``TELEGRAM_CALLBACK_TIMEOUT_SECONDS`` (default 60).
       If the operator clicks a button, log it and exit. Otherwise, exit
       gracefully after the timeout.

NOT a unit test — this hits live Telegram. CI never runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pandas as pd

# Make the repo root importable so ``src`` resolves when the script is
# launched as ``python scripts/test_notification.py``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_settings(*, allow_example_fallback: bool) -> ModuleType:
    """Load ``config.settings``.

    On Windows host: real ``config/settings.py`` + ``config/secrets.py``
    are present and we use them. On the dev Mac the script can also be run
    in ``--no-send`` mode for visual review; in that case we fall back to
    ``settings.py.example`` with a stubbed ``config.secrets`` (mirrors
    ``scripts/print_setups_for_day.py`` for consistency).
    """
    settings_real = _REPO_ROOT / "config" / "settings.py"
    settings_example = _REPO_ROOT / "config" / "settings.py.example"

    if settings_real.exists():
        # Standard path — Windows host with secrets in place.
        from _bootstrap import load_settings as _bootstrap_load_settings  # noqa: E402

        return _bootstrap_load_settings()

    if not allow_example_fallback:
        print(
            "ERROR: config/settings.py is missing. Copy it from "
            "config/settings.py.example and fill in secrets, OR re-run with "
            "--no-send to use the example values for visual-review only.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if not settings_example.exists():
        print("ERROR: config/settings.py.example is missing.", file=sys.stderr)
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
    return module


from src.detection.liquidity import (  # noqa: E402
    asian_range_to_marked_levels,
    daily_levels_to_marked_levels,
    equal_level_to_marked_level,
    find_equal_highs_lows,
    mark_asian_range,
    mark_pdh_pdl,
    mark_swing_levels,
    paris_session_to_utc,
    swing_level_to_marked_level,
)
from src.detection.setup import build_setup_candidates  # noqa: E402
from src.journal.db import get_engine, init_db, session_scope  # noqa: E402
from src.journal.repository import (  # noqa: E402
    get_decision,
    get_setup,
    insert_decision,
    insert_setup,
    setup_uid_for,
)
from src.notification.chart_renderer import render_setup_chart  # noqa: E402
from src.notification.message_formatter import format_setup_message  # noqa: E402
from src.notification.telegram_bot import TelegramNotifier  # noqa: E402

_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "historical"

# Default sample setup — XAUUSD A-grade short, 2026-01-02 NY.
# Picked because it has clean structural sweep + FVG+OB + strong displacement.
_DEFAULT_PAIR = "XAUUSD"
_DEFAULT_DATE = date(2026, 1, 2)


def _build_sample_setup(settings, pair: str, target_date: date):
    """Run the orchestrator on a real fixture and return the first A-grade setup.

    Falls back to the first setup of any grade if no A-grade is present.
    Returns None if the fixture produces no setups at all.
    """
    fixtures = {}
    for tf in ("D1", "H4", "H1", "M5"):
        path = _FIXTURE_DIR / f"{pair}_{tf}.parquet"
        if not path.exists():
            print(f"ERROR: missing fixture {path}", file=sys.stderr)
            raise SystemExit(2)
        fixtures[tf] = pd.read_parquet(path)

    setups = build_setup_candidates(
        df_h4=fixtures["H4"],
        df_h1=fixtures["H1"],
        df_m5=fixtures["M5"],
        df_d1=fixtures["D1"],
        target_date=target_date,
        symbol=pair,
        settings=settings,
    )
    if not setups:
        return None, fixtures
    a_setups = [s for s in setups if s.quality in ("A", "A+")]
    chosen = a_setups[0] if a_setups else setups[0]
    return chosen, fixtures


def _build_levels_for_setup(settings, fixtures, target_date: date, pair: str, killzone: str):
    """Re-derive the MarkedLevel list the orchestrator used for ``setup``.

    The chart renderer wants the same liquidity overlay the detection
    pipeline saw. We rebuild it here rather than threading levels out of
    ``build_setup_candidates``.
    """
    instr_cfg = settings.INSTRUMENT_CONFIG[pair]
    kz_session = settings.KILLZONE_LONDON if killzone == "london" else settings.KILLZONE_NY
    kz_start_utc, _ = paris_session_to_utc(target_date, kz_session)

    asian = mark_asian_range(fixtures["M5"], target_date, settings.SESSION_ASIA)
    daily = mark_pdh_pdl(fixtures["D1"], target_date)
    swings = mark_swing_levels(
        fixtures["H4"],
        fixtures["H1"],
        as_of_utc=kz_start_utc,
        lookback_h4=settings.SWING_LOOKBACK_H4,
        lookback_h1=settings.SWING_LOOKBACK_H1,
        min_amplitude_atr_mult_h4=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H4,
        min_amplitude_atr_mult_h1=settings.MIN_SWING_AMPLITUDE_ATR_MULT_H1,
        n_swings=settings.SWING_LEVELS_LOOKBACK_COUNT,
        h4_h1_time_tolerance_h4_candles=settings.H4_H1_TIME_TOLERANCE_CANDLES_H4,
        h4_h1_price_tolerance_fraction=settings.H4_H1_PRICE_TOLERANCE_FRACTION,
    )
    equals = find_equal_highs_lows(swings, equal_hl_tolerance=instr_cfg["equal_hl_tolerance"])
    return (
        asian_range_to_marked_levels(asian)
        + daily_levels_to_marked_levels(daily)
        + [swing_level_to_marked_level(s) for s in swings]
        + [equal_level_to_marked_level(e) for e in equals]
    )


async def _run(args) -> int:
    settings = _load_settings(allow_example_fallback=args.no_send)

    pair = args.pair or _DEFAULT_PAIR
    target_date = date.fromisoformat(args.date) if args.date else _DEFAULT_DATE

    print(f"Building setup from fixture: {pair} {target_date.isoformat()}")
    setup, fixtures = _build_sample_setup(settings, pair, target_date)
    if setup is None:
        print(
            f"ERROR: no setup detected for {pair} {target_date} — try a " "different (date, pair).",
            file=sys.stderr,
        )
        return 1

    print(
        f"Selected setup: {setup.symbol} {setup.direction.upper()} "
        f"quality={setup.quality} timestamp_utc={setup.timestamp_utc.isoformat()}"
    )

    levels = _build_levels_for_setup(settings, fixtures, target_date, pair, setup.killzone)

    chart_dir = Path(settings.CHART_OUTPUT_DIR)
    chart_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = setup.timestamp_utc.strftime("%Y%m%dT%H%M%SZ")
    chart_path = chart_dir / f"{setup.symbol}_{safe_ts}_{setup.quality.replace('+', 'plus')}.png"

    render_setup_chart(
        setup=setup,
        df_m5=fixtures["M5"],
        marked_levels=levels,
        output_path=chart_path,
        lookback_candles=getattr(settings, "CHART_LOOKBACK_CANDLES_M5", 80),
        lookforward_candles=getattr(settings, "CHART_LOOKFORWARD_CANDLES_M5", 10),
    )
    print(f"Chart written: {chart_path}")

    caption = format_setup_message(setup)
    print()
    print("---- Telegram caption (HTML) ----")
    print(caption)
    print("---------------------------------")
    print()

    if args.no_send:
        print("--no-send specified — skipping Telegram send.")
        return 0

    # Sprint 5: persist the setup before sending so the eventual button
    # callback can attach a decision row. Idempotent on setup_uid — safe
    # to re-run the script with the same fixture.
    db_path = getattr(settings, "DB_PATH", "data/journal.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine(db_path)
    init_db(engine)

    setup_uid = setup_uid_for(setup)
    with session_scope(engine) as s:
        insert_setup(s, setup, was_notified=True)
    print(f"Journal: setup persisted (uid={setup_uid}) — db={db_path}")

    received: dict[str, tuple[str, str, datetime]] = {}

    def on_callback(decision: str, sid: str, ts: datetime) -> None:
        received["payload"] = (decision, sid, ts)
        print(f"\nCallback received: decision={decision!r} sid={sid!r} ts={ts.isoformat()}")
        try:
            with session_scope(engine) as ss:
                # Defensive: scripts may send a setup that wasn't inserted
                # earlier (e.g. callback fires before the insert flushed).
                if get_setup(ss, sid) is None:
                    insert_setup(ss, setup, was_notified=True)
                if get_decision(ss, sid) is None:
                    insert_decision(ss, sid, decision, ts)
                    print(f"Journal: decision persisted (uid={sid}, decision={decision})")
                else:
                    print(f"Journal: decision already exists for uid={sid}, ignoring.")
        except Exception as exc:  # noqa: BLE001 — surface to operator, don't crash bot
            print(f"Journal write failed: {exc!r}", file=sys.stderr)

    notifier = TelegramNotifier(
        bot_token=str(settings.TELEGRAM_BOT_TOKEN),
        chat_id=int(settings.TELEGRAM_CHAT_ID),
        on_callback=on_callback,
    )

    timeout = int(getattr(settings, "TELEGRAM_CALLBACK_TIMEOUT_SECONDS", 60))

    try:
        await notifier.send_setup(setup, chart_path)
        print(
            f"Sent. Polling {timeout}s for button callbacks — press Taken or "
            "Skipped in Telegram to validate the round-trip."
        )
        await notifier.start_polling()
        try:
            for _ in range(timeout):
                await asyncio.sleep(1)
                if "payload" in received:
                    break
        finally:
            await notifier.stop()
    except Exception as exc:  # noqa: BLE001 — surface to operator
        print(f"ERROR: notification flow failed: {exc!r}", file=sys.stderr)
        return 1

    if "payload" not in received:
        print(
            f"\nNo callback received within {timeout}s. The send half is "
            "validated; click round-trip is unverified — re-run if needed."
        )
    else:
        print("\nEnd-to-end test OK.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--pair",
        choices=("XAUUSD", "NDX100", "EURUSD", "GBPUSD"),
        help="Symbol to use (default: XAUUSD).",
    )
    parser.add_argument(
        "--date",
        help="Trading date in ISO format (default: 2026-01-02).",
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="Render chart + print caption only; do not contact Telegram.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
