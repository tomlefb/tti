"""Streamlit journal dashboard (Sprint 5).

Run with::

    streamlit run dashboard.py

Read-only — the dashboard never mutates the journal. It pulls the DB
path from the ``DB_PATH`` env var if set, otherwise from
``config.settings`` (with ``config/settings.py.example`` as a fallback
on the dev Mac so the dashboard can be eyeballed without secrets).

Sections:

    1. Header KPIs (total setups, last-7d / last-30d, notification rate,
       decision rate).
    2. Per-pair stats table.
    3. Per-quality stats table.
    4. Recent-setups table (latest 20 within filters).
    5. Outcome distribution stacked bar (by quality).
    6. Sidebar filters: date range, pair, quality, decision.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pandas as pd
import streamlit as st
from sqlalchemy import select

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.journal.db import get_engine, init_db, session_scope  # noqa: E402
from src.journal.models import DecisionRow, OutcomeRow, SetupRow  # noqa: E402

PAIRS = ["XAUUSD", "NDX100", "EURUSD", "GBPUSD"]
QUALITIES = ["A+", "A", "B"]
DECISIONS = ["all", "taken", "skipped", "no_decision"]


def _resolve_db_path() -> str:
    """Resolve the journal SQLite path.

    Priority:
        1. ``DB_PATH`` env var
        2. ``config.settings.DB_PATH`` (real settings — Windows host)
        3. ``config.settings.py.example.DB_PATH`` (dev fallback)
    """
    env_path = os.environ.get("DB_PATH")
    if env_path:
        return env_path

    settings_real = _REPO_ROOT / "config" / "settings.py"
    settings_example = _REPO_ROOT / "config" / "settings.py.example"

    if settings_real.exists():
        loader = SourceFileLoader("_dashboard_settings", str(settings_real))
        module = ModuleType(loader.name)
        loader.exec_module(module)
        return str(module.DB_PATH)

    if settings_example.exists():
        # ``settings.py.example`` imports from ``config.secrets`` which is
        # gitignored — stub the import before exec.
        if "config.secrets" not in sys.modules:
            stub = ModuleType("config.secrets")
            for name in (
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID",
                "MT5_LOGIN",
                "MT5_PASSWORD",
                "MT5_SERVER",
            ):
                setattr(stub, name, None)
            sys.modules["config.secrets"] = stub
        loader = SourceFileLoader("_dashboard_settings_example", str(settings_example))
        module = ModuleType(loader.name)
        loader.exec_module(module)
        return str(module.DB_PATH)

    return "data/journal.db"


@st.cache_resource
def _get_engine_cached(db_path: str):
    """Streamlit-cached engine factory — one engine per (process, path)."""
    engine = get_engine(db_path)
    init_db(engine)
    return engine


def _load_dataframe(engine) -> pd.DataFrame:
    """Pull the joined setups + decisions + outcomes view."""
    with session_scope(engine) as s:
        rows = s.execute(
            select(SetupRow, DecisionRow, OutcomeRow)
            .outerjoin(DecisionRow, DecisionRow.setup_uid == SetupRow.setup_uid)
            .outerjoin(OutcomeRow, OutcomeRow.setup_uid == SetupRow.setup_uid)
            .order_by(SetupRow.timestamp_utc.desc())
        ).all()

        records = []
        for setup_row, dec_row, out_row in rows:
            try:
                conf = json.loads(setup_row.confluences)
            except (TypeError, ValueError):
                conf = []
            records.append(
                {
                    "setup_uid": setup_row.setup_uid,
                    "timestamp_utc": setup_row.timestamp_utc,
                    "symbol": setup_row.symbol,
                    "killzone": setup_row.killzone,
                    "direction": setup_row.direction,
                    "quality": setup_row.quality,
                    "tp_runner_rr": setup_row.tp_runner_rr,
                    "tp1_rr": setup_row.tp1_rr,
                    "was_notified": bool(setup_row.was_notified),
                    "rejection_reason": setup_row.rejection_reason,
                    "confluences": ", ".join(conf) if conf else "",
                    "decision": dec_row.decision if dec_row is not None else None,
                    "decided_at": dec_row.decided_at if dec_row is not None else None,
                    "exit_reason": out_row.exit_reason if out_row is not None else None,
                    "realized_r": out_row.realized_r if out_row is not None else None,
                    "realized_pnl_usd": out_row.realized_pnl_usd if out_row is not None else None,
                }
            )
    return pd.DataFrame.from_records(records)


def _apply_filters(
    df: pd.DataFrame,
    *,
    date_start,
    date_end,
    pairs: list[str],
    qualities: list[str],
    decision: str,
) -> pd.DataFrame:
    out = df.copy()
    if "timestamp_utc" in out.columns and len(out):
        ts = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
        if date_start is not None:
            start = pd.Timestamp(date_start).tz_localize("UTC")
            out = out[ts >= start]
            ts = ts[ts >= start]
        if date_end is not None:
            end = pd.Timestamp(date_end).tz_localize("UTC") + pd.Timedelta(days=1)
            out = out[ts < end]
    if pairs:
        out = out[out["symbol"].isin(pairs)]
    if qualities:
        out = out[out["quality"].isin(qualities)]
    if decision == "taken":
        out = out[out["decision"] == "taken"]
    elif decision == "skipped":
        out = out[out["decision"] == "skipped"]
    elif decision == "no_decision":
        out = out[out["decision"].isna()]
    return out


def _render_kpi_header(df: pd.DataFrame, full: pd.DataFrame) -> None:
    now = datetime.now(UTC)
    last_7d_cutoff = pd.Timestamp(now - timedelta(days=7))
    last_30d_cutoff = pd.Timestamp(now - timedelta(days=30))

    if len(full):
        ts_full = pd.to_datetime(full["timestamp_utc"], utc=True, errors="coerce")
        last_7d = int((ts_full >= last_7d_cutoff).sum())
        last_30d = int((ts_full >= last_30d_cutoff).sum())
    else:
        last_7d = last_30d = 0

    total = len(full)
    notified = int(full["was_notified"].sum()) if total else 0
    decided = int(full["decision"].notna().sum()) if total else 0

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total setups", total)
    col2.metric("Last 7d", last_7d)
    col3.metric("Last 30d", last_30d)
    col4.metric(
        "Notification rate",
        f"{(notified / total * 100):.1f}%" if total else "—",
    )
    col5.metric(
        "Decision rate",
        f"{(decided / notified * 100):.1f}%" if notified else "—",
    )


def _aggregate(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    """Aggregate stats per ``group_col`` for the dashboard tables."""
    if df.empty:
        return pd.DataFrame()

    groups = df.groupby(group_col, sort=False)
    out = pd.DataFrame(
        {
            "Detected": groups.size(),
            "Notified": groups["was_notified"].sum(),
            "Taken": groups["decision"].apply(lambda s: int((s == "taken").sum())),
            "Skipped": groups["decision"].apply(lambda s: int((s == "skipped").sum())),
            "Wins": groups["exit_reason"].apply(
                lambda s: int(s.isin(["tp1_hit", "tp_runner_hit"]).sum())
            ),
            "Losses": groups["exit_reason"].apply(lambda s: int((s == "sl_hit").sum())),
            "Mean R": groups["realized_r"].mean(),
            "Total R": groups["realized_r"].sum(),
        }
    ).reset_index()
    out["Win rate"] = out.apply(
        lambda row: (
            f"{row['Wins'] / (row['Wins'] + row['Losses']) * 100:.1f}%"
            if (row["Wins"] + row["Losses"]) > 0
            else "—"
        ),
        axis=1,
    )
    out["Mean R"] = out["Mean R"].apply(lambda v: f"{v:.2f}" if pd.notna(v) else "—")
    out["Total R"] = out["Total R"].apply(lambda v: f"{v:.2f}" if pd.notna(v) else "—")
    return out


def _render_outcome_distribution(df: pd.DataFrame) -> None:
    if df.empty or df["exit_reason"].isna().all():
        st.info("No outcomes yet. Run the outcome tracker after some live trades.")
        return

    pivot = (
        df.assign(exit_reason=df["exit_reason"].fillna("no_outcome"))
        .pivot_table(
            index="quality",
            columns="exit_reason",
            values="setup_uid",
            aggfunc="count",
            fill_value=0,
        )
        .reindex(QUALITIES, fill_value=0)
    )
    st.bar_chart(pivot)


def main() -> None:
    st.set_page_config(page_title="TJR Journal", layout="wide")
    st.title("TJR Trading Journal")
    st.caption(
        "Sprint 5 dashboard — read-only view of detected setups, decisions, "
        "and reconciled MT5 outcomes."
    )

    db_path = _resolve_db_path()
    st.sidebar.write(f"**DB path**: `{db_path}`")

    engine = _get_engine_cached(db_path)
    df_full = _load_dataframe(engine)

    # --- Sidebar filters -----------------------------------------------
    st.sidebar.header("Filters")
    default_end = datetime.now(UTC).date()
    default_start = default_end - timedelta(days=30)
    date_range = st.sidebar.date_input(
        "Date range",
        value=(default_start, default_end),
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        date_start, date_end = date_range
    else:
        date_start, date_end = default_start, default_end

    selected_pairs = st.sidebar.multiselect("Pairs", PAIRS, default=PAIRS)
    selected_qualities = st.sidebar.multiselect("Quality", QUALITIES, default=QUALITIES)
    selected_decision = st.sidebar.selectbox("Decision", DECISIONS, index=0)

    df = _apply_filters(
        df_full,
        date_start=date_start,
        date_end=date_end,
        pairs=selected_pairs,
        qualities=selected_qualities,
        decision=selected_decision,
    )

    _render_kpi_header(df, df_full)
    st.divider()

    # --- Per-pair / per-quality tables ---------------------------------
    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("Per pair")
        pair_table = _aggregate(df, "symbol")
        if pair_table.empty:
            st.info("No setups match current filters.")
        else:
            st.dataframe(pair_table, use_container_width=True, hide_index=True)
    with col_right:
        st.subheader("Per quality")
        q_table = _aggregate(df, "quality")
        if q_table.empty:
            st.info("No setups match current filters.")
        else:
            st.dataframe(q_table, use_container_width=True, hide_index=True)

    # --- Outcome distribution ------------------------------------------
    st.subheader("Outcome distribution by quality")
    _render_outcome_distribution(df)

    # --- Recent setups -------------------------------------------------
    st.subheader("Recent setups")
    if df.empty:
        st.info("No setups match current filters.")
    else:
        recent = df.head(20).copy()
        recent["timestamp_utc"] = pd.to_datetime(recent["timestamp_utc"], utc=True, errors="coerce")
        st.dataframe(
            recent[
                [
                    "timestamp_utc",
                    "symbol",
                    "killzone",
                    "direction",
                    "quality",
                    "tp_runner_rr",
                    "was_notified",
                    "decision",
                    "exit_reason",
                    "realized_r",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )


if __name__ == "__main__":
    main()
