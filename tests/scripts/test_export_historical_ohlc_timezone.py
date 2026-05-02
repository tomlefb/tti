"""Unit tests for the broker-time → UTC conversion in the MT5 exporter.

The conversion is the load-bearing fix from the timezone audit
(``calibration/runs/timezone_audit_2026-05-02T16-04-57Z``). It must:

* Apply ``-3h`` during EU DST (EEST = UTC+3).
* Apply ``-2h`` outside DST (EET = UTC+2).
* Cross the autumn fall-back transition (last Sunday of October)
  without losing rows or duplicating timestamps.
* Cross the spring forward-transition (last Sunday of March) without
  raising on the non-existent wallclock hour.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

# Add ``scripts/`` to sys.path so the exporter module is importable.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from export_historical_ohlc import _broker_seconds_series_to_utc  # noqa: E402


def _broker_wallclock_to_seconds(broker_dt: datetime) -> float:
    """Encode a naive broker wallclock as MT5 would: ``datetime(...).timestamp()``
    where the wallclock components are reinterpreted as if UTC."""
    if broker_dt.tzinfo is not None:
        raise AssertionError("broker_dt must be naive (broker local wallclock)")
    return broker_dt.replace(tzinfo=UTC).timestamp()


class TestBrokerSecondsToUtc:
    def test_summer_dst_subtracts_3h(self):
        # 15 Aug 2025 (deep summer in EU; EEST = UTC+3).
        broker = datetime(2025, 8, 15, 14, 0, 0)
        s = _broker_wallclock_to_seconds(broker)
        result = _broker_seconds_series_to_utc(pd.Series([s]))
        expected = pd.Timestamp("2025-08-15 11:00:00", tz="UTC")
        assert result.iloc[0] == expected

    def test_winter_eet_subtracts_2h(self):
        # 15 Dec 2025 (EET = UTC+2).
        broker = datetime(2025, 12, 15, 14, 0, 0)
        s = _broker_wallclock_to_seconds(broker)
        result = _broker_seconds_series_to_utc(pd.Series([s]))
        expected = pd.Timestamp("2025-12-15 12:00:00", tz="UTC")
        assert result.iloc[0] == expected

    def test_returns_utc_dtype(self):
        broker = datetime(2025, 8, 15, 14, 0, 0)
        result = _broker_seconds_series_to_utc(
            pd.Series([_broker_wallclock_to_seconds(broker)])
        )
        assert str(result.dtype) == "datetime64[ns, UTC]"

    def test_preserves_length(self):
        broker_starts = [
            datetime(2025, 6, 1, 9, 0, 0),
            datetime(2025, 6, 1, 9, 5, 0),
            datetime(2025, 6, 1, 9, 10, 0),
            datetime(2025, 6, 1, 9, 15, 0),
        ]
        s = pd.Series([_broker_wallclock_to_seconds(d) for d in broker_starts])
        result = _broker_seconds_series_to_utc(s)
        assert len(result) == len(s)

    def test_fall_back_transition_october_2025(self):
        # Last Sunday of October 2025 = 2025-10-26.
        # EU clocks fall back from 04:00 EEST to 03:00 EET. FX and index
        # markets are closed during the ambiguous hour (Sunday early
        # morning), so MT5 does not emit duplicate-wallclock bars in
        # practice — we never need to disambiguate within that hour.
        # The realistic test is that a Friday-pre-transition bar and a
        # Monday-post-transition bar with identical broker wallclock
        # convert to UTC instants one hour apart (because the offset
        # changed from +3 to +2 across the weekend).
        friday_pre  = datetime(2025, 10, 24, 14, 0)  # EEST = UTC+3
        monday_post = datetime(2025, 10, 27, 14, 0)  # EET  = UTC+2
        s = pd.Series(
            [
                _broker_wallclock_to_seconds(friday_pre),
                _broker_wallclock_to_seconds(monday_post),
            ]
        )
        result = _broker_seconds_series_to_utc(s)
        assert result.iloc[0] == pd.Timestamp("2025-10-24 11:00:00", tz="UTC")
        assert result.iloc[1] == pd.Timestamp("2025-10-27 12:00:00", tz="UTC")
        # The wallclock distance is exactly 3 days (+72h); the UTC
        # distance must be 73h because the broker fell back one hour.
        delta = result.iloc[1] - result.iloc[0]
        assert delta == pd.Timedelta(hours=73)

    def test_spring_forward_transition_march_2026(self):
        # Last Sunday of March 2026 = 2026-03-29.
        # EU clocks spring forward from 03:00 EET to 04:00 EEST. The
        # wallclock 03:00..03:55 does not exist on that day. MT5 should
        # not emit such bars (markets are closed on Sunday morning), but
        # if it does, the helper must not raise.
        broker_wallclocks = [
            datetime(2026, 3, 29, 2, 55),
            datetime(2026, 3, 29, 3, 30),  # nonexistent (skipped by DST)
            datetime(2026, 3, 29, 4, 0),
            datetime(2026, 3, 29, 4, 5),
        ]
        s = pd.Series([_broker_wallclock_to_seconds(b) for b in broker_wallclocks])
        # Must not raise; nonexistent="shift_forward" pushes the gap
        # bar to the next valid wallclock.
        result = _broker_seconds_series_to_utc(s)
        assert len(result) == len(s)
        assert str(result.dtype) == "datetime64[ns, UTC]"

    def test_summer_winter_offsets_differ_by_one_hour(self):
        # Same broker wallclock 14:00 in summer vs winter must differ
        # by exactly one hour in true UTC (DST adds an hour to the
        # offset Apr-Oct).
        summer = datetime(2025, 8, 15, 14, 0, 0)
        winter = datetime(2025, 12, 15, 14, 0, 0)
        result = _broker_seconds_series_to_utc(
            pd.Series(
                [
                    _broker_wallclock_to_seconds(summer),
                    _broker_wallclock_to_seconds(winter),
                ]
            )
        )
        # Summer should be at 11:00 UTC, winter at 12:00 UTC.
        delta = result.iloc[1].time().hour - result.iloc[0].time().hour
        assert delta == 1

    def test_handles_array_input(self):
        # ``seconds_like`` accepts plain lists / numpy arrays, not just Series.
        broker = datetime(2025, 8, 15, 14, 0, 0)
        seconds_array = [_broker_wallclock_to_seconds(broker)]
        result = _broker_seconds_series_to_utc(seconds_array)
        assert isinstance(result, pd.Series)
        assert result.iloc[0] == pd.Timestamp("2025-08-15 11:00:00", tz="UTC")

    @pytest.mark.parametrize(
        ("broker_wallclock", "expected_utc"),
        [
            # July 2025 (DST), full summer.
            (datetime(2025, 7, 1, 9, 0), pd.Timestamp("2025-07-01 06:00", tz="UTC")),
            # September 2025 (DST), late summer.
            (datetime(2025, 9, 15, 17, 30), pd.Timestamp("2025-09-15 14:30", tz="UTC")),
            # December 2025 (no DST).
            (datetime(2025, 12, 15, 9, 0), pd.Timestamp("2025-12-15 07:00", tz="UTC")),
            # February 2026 (no DST), Friday close.
            (datetime(2026, 2, 27, 21, 55), pd.Timestamp("2026-02-27 19:55", tz="UTC")),
        ],
    )
    def test_known_pairs_per_regime(self, broker_wallclock, expected_utc):
        s = pd.Series([_broker_wallclock_to_seconds(broker_wallclock)])
        result = _broker_seconds_series_to_utc(s)
        assert result.iloc[0] == expected_utc
