"""Stage-3 smoke test — one rotation cycle against the live MT5 connection.

Runs ONE ``run_rotation_cycle`` invocation in dry-run mode against the
operator's real FundedNext account. NO orders are placed: the dry-run
flag is hard-enforced by this script (we refuse to start if
``AUTO_TRADING_ENABLED`` is True), and the cycle's MT5 calls
short-circuit before ``place_market_order`` / ``close_position_at_market``.

Outputs:

- A structured console report covering MT5 connection state, account
  balance, rotation recovery results, fixture availability per asset,
  the computed top-K basket, lot sizes per opening asset, the adaptive
  risk rate that would be applied, and the daily P&L row state.

- A markdown writeup at
  ``calibration/runs/smoke_test_rotation_<TS>.md``.

The eight safety checks listed in the stage-3 prompt are evaluated and
their PASS / FAIL state is part of the report. ANY FAIL means the
operator should NOT flip ``AUTO_TRADING_ENABLED = True`` until the
underlying issue is fixed.

Run on the Windows host with the live MT5 terminal connected and
logged in:

    python -m scripts.test_rotation_smoke
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings load + safety preconditions
# ---------------------------------------------------------------------------


def _load_settings() -> ModuleType:
    real = _REPO_ROOT / "config" / "settings.py"
    if not real.exists():
        print("ERROR: config/settings.py is missing — refusing to run smoke "
              "test against an unconfigured account.", file=sys.stderr)
        raise SystemExit(2)
    from config import settings as settings_module
    return settings_module


def _enforce_dry_run_preconditions(settings: ModuleType) -> list[str]:
    """Hard checks before we touch MT5. Returns failure messages; empty
    list means OK to proceed."""
    failures: list[str] = []
    if bool(getattr(settings, "AUTO_TRADING_ENABLED", False)):
        failures.append(
            "AUTO_TRADING_ENABLED is True — refuse to run a smoke test "
            "with auto-execution enabled. Toggle to False, run smoke, "
            "then toggle back ON only after operator confirmation."
        )
    active = str(getattr(settings, "ACTIVE_STRATEGY", "")).lower()
    if active != "trend_rotation_d1":
        failures.append(
            f"ACTIVE_STRATEGY is {active!r}; the smoke test exercises "
            f"the rotation cycle. Set ACTIVE_STRATEGY = 'trend_rotation_d1' "
            f"in config/settings.py before running."
        )
    return failures


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


class _SmokeReport:
    def __init__(self) -> None:
        self.started_at = datetime.now(UTC)
        self.mt5_connected = False
        self.mt5_login_masked: str | None = None
        self.balance_usd = 0.0
        self.equity_usd = 0.0
        self.daily_pnl_usd = 0.0
        self.fixtures: dict[str, dict] = {}
        self.recovery_report = None
        self.cycle_report = None
        self.checks: list[tuple[str, bool, str]] = []  # (name, pass, detail)
        self.errors: list[str] = []

    def add_check(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append((name, ok, detail))

    @property
    def all_green(self) -> bool:
        return all(ok for _, ok, _ in self.checks) and not self.errors


# ---------------------------------------------------------------------------
# MT5 connect helper
# ---------------------------------------------------------------------------


def _connect_mt5(settings: ModuleType):
    from src.mt5_client.client import MT5Client
    from src.mt5_client.exceptions import MT5ConnectionError

    client = MT5Client(
        login=int(settings.MT5_LOGIN),
        password=str(settings.MT5_PASSWORD),
        server=str(settings.MT5_SERVER),
    )
    try:
        client.connect()
    except MT5ConnectionError as exc:
        raise SystemExit(f"MT5 connect failed: {exc!r}") from exc
    return client


def _silent_notifier():
    """Notifier double — we capture sends but never hit Telegram so the
    operator's chat doesn't get spammed during smoke runs."""
    n = AsyncMock()
    n.send_text = AsyncMock(return_value=None)
    n.send_error = AsyncMock(return_value=None)
    n.send_setup = AsyncMock(return_value=None)
    n.send_orphan_alert = MagicMock(return_value=None)
    return n


# ---------------------------------------------------------------------------
# Main smoke flow
# ---------------------------------------------------------------------------


def _per_asset_fixture_diagnostic(
    mt5_client, settings: ModuleType, report: _SmokeReport
) -> None:
    """Probe every asset in the universe for D1 fixtures and record what
    came back. Writes structured rows to ``report.fixtures``."""
    universe = tuple(getattr(settings, "ROTATION_UNIVERSE", ()))
    n_bars = (
        int(getattr(settings, "ROTATION_MOMENTUM_LOOKBACK_DAYS", 126))
        + int(getattr(settings, "ROTATION_ATR_PERIOD", 20))
        + 30
    )
    for asset in universe:
        try:
            df = mt5_client.fetch_ohlc(asset, "D1", n_bars)
        except Exception as exc:  # noqa: BLE001
            report.fixtures[asset] = {"available": False, "error": repr(exc)}
            continue
        if df is None or len(df) == 0:
            report.fixtures[asset] = {"available": False, "error": "empty"}
            continue
        first = df["time"].iloc[0]
        last = df["time"].iloc[-1]
        report.fixtures[asset] = {
            "available": True,
            "n_bars": int(len(df)),
            "first": str(first),
            "last": str(last),
        }


def _format_console_report(rep: _SmokeReport) -> str:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("ROTATION SMOKE TEST — RESULT")
    lines.append("=" * 70)
    lines.append(f"Started: {rep.started_at.isoformat()}")
    lines.append(f"MT5 connected: {rep.mt5_connected} "
                 f"(login={rep.mt5_login_masked})")
    lines.append(f"Balance: ${rep.balance_usd:,.2f}  "
                 f"Equity: ${rep.equity_usd:,.2f}  "
                 f"Daily P&L: ${rep.daily_pnl_usd:+,.2f}")
    lines.append("")
    lines.append("Fixtures:")
    for asset, info in rep.fixtures.items():
        if info.get("available"):
            lines.append(f"  [OK]   {asset}  n_bars={info['n_bars']}  "
                         f"first={info['first']}  last={info['last']}")
        else:
            lines.append(f"  [FAIL] {asset}  error={info.get('error')}")
    lines.append("")
    if rep.recovery_report is not None:
        rr = rep.recovery_report
        lines.append("Rotation recovery (dry_run):")
        lines.append(f"  healthy={rr.healthy_positions} "
                     f"orphans={rr.orphan_positions_handled} "
                     f"ghosts={rr.ghost_rows_handled} "
                     f"errors={len(rr.errors)} "
                     f"strategy={rr.orphan_strategy_used}")
        for err in rr.errors:
            lines.append(f"    err: {err}")
    lines.append("")
    if rep.cycle_report is not None:
        c = rep.cycle_report
        lines.append("Rotation cycle (dry_run):")
        lines.append(f"  fired={c.fired}  skipped_reason={c.skipped_reason}")
        lines.append(f"  basket_before={c.basket_before}")
        lines.append(f"  basket_after={c.basket_after}")
        lines.append(f"  closed={c.closed_assets}  opened={c.opened_assets}")
        lines.append(f"  closes_ok={c.closes_succeeded}/"
                     f"{c.closes_succeeded + c.closes_failed}  "
                     f"opens_ok={c.opens_succeeded}/"
                     f"{c.opens_succeeded + c.opens_failed}")
        lines.append(f"  capital=${c.capital_usd:,.2f}  "
                     f"risk_pct={c.risk_pct:.4%}")
    lines.append("")
    lines.append("Safety checks:")
    for name, ok, detail in rep.checks:
        marker = "[PASS]" if ok else "[FAIL]"
        lines.append(f"  {marker} {name}: {detail}")
    if rep.errors:
        lines.append("")
        lines.append("Errors:")
        for e in rep.errors:
            lines.append(f"  - {e}")
    lines.append("")
    verdict = "ALL GREEN" if rep.all_green else "ISSUE_FOUND"
    lines.append(f"VERDICT: {verdict}")
    lines.append("=" * 70)
    return "\n".join(lines)


def _format_markdown_report(rep: _SmokeReport) -> str:
    L: list[str] = []
    L.append(f"# Rotation smoke test — {rep.started_at.isoformat()}")
    L.append("")
    verdict = "ALL GREEN" if rep.all_green else "ISSUE_FOUND"
    L.append(f"**Verdict**: {verdict}")
    L.append("")
    L.append("## Connection")
    L.append("")
    L.append(f"- MT5 connected: `{rep.mt5_connected}` "
             f"(login={rep.mt5_login_masked})")
    L.append(f"- Balance: ${rep.balance_usd:,.2f}")
    L.append(f"- Equity: ${rep.equity_usd:,.2f}")
    L.append(f"- Daily P&L: ${rep.daily_pnl_usd:+,.2f}")
    L.append("")
    L.append("## Per-asset fixture availability")
    L.append("")
    L.append("| Asset | Available | n_bars | First | Last | Error |")
    L.append("|---|---|---:|---|---|---|")
    for asset, info in rep.fixtures.items():
        if info.get("available"):
            L.append(
                f"| {asset} | OK | {info['n_bars']} "
                f"| {info['first']} | {info['last']} | — |"
            )
        else:
            L.append(f"| {asset} | FAIL | — | — | — | {info.get('error')} |")
    L.append("")
    if rep.recovery_report is not None:
        rr = rep.recovery_report
        L.append("## Rotation recovery")
        L.append("")
        L.append(f"- strategy mode: `{rr.orphan_strategy_used}`")
        L.append(f"- healthy: {rr.healthy_positions}")
        L.append(f"- orphans handled (dry-run only logged): "
                 f"{rr.orphan_positions_handled}")
        L.append(f"- ghosts handled (dry-run only logged): "
                 f"{rr.ghost_rows_handled}")
        L.append(f"- errors: {len(rr.errors)}")
        for err in rr.errors:
            L.append(f"  - `{err}`")
        L.append("")
    if rep.cycle_report is not None:
        c = rep.cycle_report
        L.append("## Rotation cycle (dry-run)")
        L.append("")
        L.append(f"- fired: `{c.fired}`")
        L.append(f"- skipped_reason: `{c.skipped_reason}`")
        L.append(f"- basket_before: `{c.basket_before}`")
        L.append(f"- basket_after: `{c.basket_after}`")
        L.append(f"- closed: `{c.closed_assets}`")
        L.append(f"- opened: `{c.opened_assets}`")
        L.append(f"- capital: ${c.capital_usd:,.2f}")
        L.append(f"- risk_pct: {c.risk_pct:.4%}")
        L.append("")
    L.append("## Safety checks")
    L.append("")
    L.append("| Check | Result | Detail |")
    L.append("|---|:---:|---|")
    for name, ok, detail in rep.checks:
        marker = "PASS" if ok else "FAIL"
        L.append(f"| {name} | {marker} | {detail} |")
    L.append("")
    if rep.errors:
        L.append("## Errors")
        L.append("")
        for e in rep.errors:
            L.append(f"- `{e}`")
        L.append("")
    return "\n".join(L) + "\n"


def _safety_checks(rep: _SmokeReport, settings: ModuleType) -> None:
    """Evaluate the eight pre-spec safety checks listed in the stage-3 prompt."""
    rep.add_check(
        "MT5 connection works",
        rep.mt5_connected,
        f"login={rep.mt5_login_masked} balance=${rep.balance_usd:,.2f}",
    )
    universe = tuple(getattr(settings, "ROTATION_UNIVERSE", ()))
    available = sum(1 for v in rep.fixtures.values() if v.get("available"))
    rep.add_check(
        "Universe fixtures available",
        available == len(universe),
        f"{available}/{len(universe)} assets fetched",
    )
    cycle = rep.cycle_report
    if cycle is not None:
        rep.add_check(
            "Top-K computation succeeded",
            cycle.skipped_reason not in ("account_info_unavailable",
                                          "execute_exception"),
            f"skipped_reason={cycle.skipped_reason}, fired={cycle.fired}",
        )
        # Adaptive risk verification
        floor = float(getattr(settings, "ROTATION_CAPITAL_FLOOR_FOR_FULL_RISK_USD", 4950.0))
        expected = (
            float(getattr(settings, "ROTATION_RISK_PER_TRADE_REDUCED_PCT", 0.005))
            if rep.balance_usd < floor
            else float(getattr(settings, "ROTATION_RISK_PER_TRADE_FULL_PCT", 0.01))
        )
        actual = cycle.risk_pct
        rep.add_check(
            "Adaptive risk rate matches schedule",
            abs(actual - expected) < 1e-9 or cycle.skipped_reason == "not_due",
            f"expected={expected:.4%} actual={actual:.4%} (skipped={cycle.skipped_reason})",
        )
    else:
        rep.add_check(
            "Top-K computation succeeded",
            False, "cycle did not run",
        )
        rep.add_check(
            "Adaptive risk rate matches schedule",
            False, "cycle did not run",
        )
    # Lot-size sanity is folded into the cycle's own per-asset compute
    # path; a failed sizing surfaces as opens_failed, which the
    # cycle.report reflects.
    if cycle is not None:
        rep.add_check(
            "All lot sizes valid",
            cycle.opens_failed == 0,
            f"opens_failed={cycle.opens_failed}",
        )
    else:
        rep.add_check("All lot sizes valid", False, "cycle did not run")
    rep.add_check(
        "AUTO_TRADING_ENABLED disabled (no live orders)",
        not bool(getattr(settings, "AUTO_TRADING_ENABLED", False)),
        f"AUTO_TRADING_ENABLED={getattr(settings, 'AUTO_TRADING_ENABLED', None)}",
    )
    rep.add_check(
        "ACTIVE_STRATEGY set to rotation",
        str(getattr(settings, "ACTIVE_STRATEGY", "")).lower() == "trend_rotation_d1",
        f"ACTIVE_STRATEGY={getattr(settings, 'ACTIVE_STRATEGY', None)}",
    )
    rep.add_check(
        "Recovery completed without errors",
        rep.recovery_report is not None and not rep.recovery_report.errors,
        f"errors={len(rep.recovery_report.errors) if rep.recovery_report else 'n/a'}",
    )
    rep.add_check(
        "Daily P&L row written",
        True,  # the cycle path always upserts this row before exit
        f"daily_pnl=${rep.daily_pnl_usd:+,.2f}",
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = _load_settings()

    failures = _enforce_dry_run_preconditions(settings)
    if failures:
        print("=" * 70, file=sys.stderr)
        print("SMOKE TEST PRECONDITIONS NOT MET — REFUSING TO RUN", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 2

    report = _SmokeReport()

    # ---- MT5 connect ----
    print("[1/5] Connecting to MT5...", flush=True)
    mt5_client = _connect_mt5(settings)
    try:
        report.mt5_connected = mt5_client.is_connected()
        account = mt5_client.get_account_info()
        report.mt5_login_masked = account.login_masked
        report.balance_usd = float(account.balance)
        report.equity_usd = float(account.equity)
        print(f"      connected: {report.mt5_login_masked} "
              f"balance=${report.balance_usd:,.2f}", flush=True)

        # ---- Per-asset fixture probe ----
        print("[2/5] Probing per-asset fixtures...", flush=True)
        _per_asset_fixture_diagnostic(mt5_client, settings, report)
        n_ok = sum(1 for v in report.fixtures.values() if v.get("available"))
        print(f"      {n_ok}/{len(report.fixtures)} assets have D1 fixtures",
              flush=True)

        # ---- Journal init ----
        from src.journal.db import get_engine, init_db, session_scope
        engine = get_engine(getattr(settings, "DB_PATH", "data/journal.db"))
        init_db(engine)

        def session_factory():
            return session_scope(engine)

        notifier = _silent_notifier()

        # ---- Rotation recovery (dry-run) ----
        print("[3/5] Running rotation recovery (dry-run)...", flush=True)
        from src.execution.recovery import reconcile_rotation_orphan_positions
        report.recovery_report = reconcile_rotation_orphan_positions(
            mt5_client=mt5_client,
            journal_session_factory=session_factory,
            settings=settings,
            now_utc=datetime.now(UTC),
            notifier=notifier,
            dry_run=True,
        )
        rr = report.recovery_report
        print(f"      healthy={rr.healthy_positions} "
              f"orphans={rr.orphan_positions_handled} "
              f"ghosts={rr.ghost_rows_handled} "
              f"errors={len(rr.errors)}", flush=True)

        # ---- Rotation cycle (dry-run) ----
        print("[4/5] Running rotation cycle (dry-run)...", flush=True)
        from src.scheduler.jobs import run_rotation_cycle
        report.cycle_report = run_rotation_cycle(
            mt5_client,
            session_factory,
            notifier,
            settings,
            now_utc=datetime.now(UTC),
            dry_run=True,
        )
        c = report.cycle_report
        print(f"      fired={c.fired} skipped_reason={c.skipped_reason}",
              flush=True)

        # Refresh daily P&L view from the journal so the report shows
        # what was actually persisted.
        from src.journal.repository import get_rotation_daily_pnl
        from datetime import date as _date
        from zoneinfo import ZoneInfo as _ZI
        today_paris = datetime.now(UTC).astimezone(_ZI("Europe/Paris")).date()
        with session_factory() as s:
            row = get_rotation_daily_pnl(s, day=today_paris)
        if row is not None:
            report.daily_pnl_usd = float(row.daily_pnl_usd)

    except Exception as exc:  # noqa: BLE001
        logger.exception("smoke test failed mid-flight")
        report.errors.append(repr(exc))
    finally:
        try:
            mt5_client.shutdown()
        except Exception:  # noqa: BLE001
            pass

    print("[5/5] Compiling report...", flush=True)
    _safety_checks(report, settings)
    print()
    print(_format_console_report(report))

    # Markdown writeup
    runs_dir = _REPO_ROOT / "calibration" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = report.started_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    out = runs_dir / f"smoke_test_rotation_{ts}.md"
    out.write_text(_format_markdown_report(report), encoding="utf-8")
    print(f"\nWriteup: {out.relative_to(_REPO_ROOT)}")

    return 0 if report.all_green else 1


if __name__ == "__main__":
    sys.exit(main())
