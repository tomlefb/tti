"""Tests for ``src.data.dukascopy.client``.

The bulk of the suite uses a mocked ``dukascopy_python.fetch`` so it runs
deterministically and offline. A handful of tests marked with
``@pytest.mark.network`` actually hit Dukascopy and are skipped by default
runs (``pytest -m "not network"``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from src.data.dukascopy.client import (
    CANONICAL_COLUMNS,
    INSTRUMENT_MAPPING,
    DukascopyClient,
    _months_between,
    canonical_instruments,
    from_dukascopy_code,
    to_dukascopy_code,
)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _make_fake_df(start: datetime, n_bars: int) -> pd.DataFrame:
    """Build a synthetic OHLCV frame the lib could plausibly return."""
    if n_bars == 0:
        idx = pd.DatetimeIndex([], tz="UTC")
        return pd.DataFrame(
            {col: pd.Series(dtype="float64") for col in CANONICAL_COLUMNS},
            index=idx,
        )
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    idx = pd.date_range(start, periods=n_bars, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.0] * n_bars,
            "high": [1.1] * n_bars,
            "low": [0.9] * n_bars,
            "close": [1.05] * n_bars,
            "volume": [100.0] * n_bars,
        },
        index=idx,
    )


# ---------------------------------------------------------------------- #
# Mapping
# ---------------------------------------------------------------------- #


class TestInstrumentMapping:
    def test_canonical_list_covers_seven_instruments(self):
        assert canonical_instruments() == sorted(
            ["XAUUSD", "NDX100", "SPX500", "EURUSD", "GBPUSD", "US30", "BTCUSD"]
        )

    @pytest.mark.parametrize(
        ("name", "code"),
        [
            ("XAUUSD", "XAU/USD"),
            ("NDX100", "E_NQ-100"),
            ("SPX500", "E_SandP-500"),
            ("EURUSD", "EUR/USD"),
            ("GBPUSD", "GBP/USD"),
            ("US30", "E_D&J-Ind"),
            ("BTCUSD", "BTC/USD"),
        ],
    )
    def test_to_dukascopy_code(self, name, code):
        assert to_dukascopy_code(name) == code

    def test_inverse_roundtrip(self):
        for name in canonical_instruments():
            assert from_dukascopy_code(to_dukascopy_code(name)) == name

    def test_unknown_instrument_raises(self):
        with pytest.raises(ValueError, match="Unknown instrument"):
            to_dukascopy_code("UNKNOWN")

    def test_unknown_code_raises(self):
        with pytest.raises(ValueError, match="Unknown Dukascopy code"):
            from_dukascopy_code("FOO/BAR")


# ---------------------------------------------------------------------- #
# Argument validation
# ---------------------------------------------------------------------- #


class TestArgumentValidation:
    def test_start_must_precede_end(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        with pytest.raises(ValueError, match="start must be < end"):
            client.fetch_m5(
                "XAUUSD",
                start=datetime(2024, 6, 14, tzinfo=timezone.utc),
                end=datetime(2024, 6, 14, tzinfo=timezone.utc),
            )

    def test_invalid_side_raises(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        with pytest.raises(ValueError, match="side must be 'bid' or 'ask'"):
            client.fetch_m5(
                "XAUUSD",
                start=datetime(2024, 6, 14, tzinfo=timezone.utc),
                end=datetime(2024, 6, 15, tzinfo=timezone.utc),
                side="mid",  # type: ignore[arg-type]
            )

    def test_unknown_instrument_raises(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        with pytest.raises(ValueError, match="Unknown instrument"):
            client.fetch_m5(
                "BANANA",
                start=datetime(2024, 6, 14, tzinfo=timezone.utc),
                end=datetime(2024, 6, 15, tzinfo=timezone.utc),
            )


# ---------------------------------------------------------------------- #
# Fetch (mocked) — output format and filtering
# ---------------------------------------------------------------------- #


class TestFetchFormat:
    def test_returns_canonical_columns_and_utc_index(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        # 600 bars covering the requested 2-day window
        fake = _make_fake_df(
            datetime(2024, 6, 14, tzinfo=timezone.utc), n_bars=600
        )
        with patch(
            "src.data.dukascopy.client.duka.fetch",
            return_value=fake,
        ) as mock_fetch:
            df = client.fetch_m5(
                "XAUUSD",
                start=datetime(2024, 6, 14, tzinfo=timezone.utc),
                end=datetime(2024, 6, 16, tzinfo=timezone.utc),
                use_cache=False,
            )
            assert mock_fetch.called
        assert list(df.columns) == CANONICAL_COLUMNS
        assert df.index.tz is not None
        assert str(df.index.tz) in ("UTC", "tzutc()")
        assert df.index.is_monotonic_increasing

    def test_window_filter_is_half_open(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        # 5 days of M5 starting Sunday — lib often returns Sunday rollover
        fake = _make_fake_df(
            datetime(2024, 6, 9, 22, tzinfo=timezone.utc), n_bars=2000
        )
        start = datetime(2024, 6, 10, tzinfo=timezone.utc)
        end = datetime(2024, 6, 11, tzinfo=timezone.utc)
        with patch(
            "src.data.dukascopy.client.duka.fetch", return_value=fake
        ):
            df = client.fetch_m5(
                "XAUUSD", start=start, end=end, use_cache=False
            )
        assert len(df) > 0
        assert df.index.min() >= start
        assert df.index.max() < end

    def test_naive_datetimes_treated_as_utc(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        fake = _make_fake_df(datetime(2024, 6, 14), n_bars=288)
        with patch(
            "src.data.dukascopy.client.duka.fetch", return_value=fake
        ) as mock_fetch:
            client.fetch_m5(
                "XAUUSD",
                start=datetime(2024, 6, 14),
                end=datetime(2024, 6, 15),
                use_cache=False,
            )
        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["start"].tzinfo is not None
        assert kwargs["end"].tzinfo is not None

    def test_empty_response_returns_canonical_empty_frame(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        with patch(
            "src.data.dukascopy.client.duka.fetch",
            return_value=pd.DataFrame(),
        ):
            df = client.fetch_m5(
                "EURUSD",
                start=datetime(2010, 1, 1, tzinfo=timezone.utc),
                end=datetime(2010, 1, 8, tzinfo=timezone.utc),
                use_cache=False,
            )
        assert list(df.columns) == CANONICAL_COLUMNS
        assert len(df) == 0
        assert df.index.tz is not None


# ---------------------------------------------------------------------- #
# Cache behaviour
# ---------------------------------------------------------------------- #


class TestCache:
    def test_second_fetch_serves_from_cache(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        # one full month of bars (every 5 min for June 2024 ≈ 8640)
        fake_month = _make_fake_df(
            datetime(2024, 6, 1, tzinfo=timezone.utc), n_bars=8640
        )

        start = datetime(2024, 6, 14, tzinfo=timezone.utc)
        end = datetime(2024, 6, 15, tzinfo=timezone.utc)
        with patch(
            "src.data.dukascopy.client.duka.fetch", return_value=fake_month
        ) as mock_first:
            df1 = client.fetch_m5("XAUUSD", start, end)
            assert mock_first.call_count == 1

        with patch(
            "src.data.dukascopy.client.duka.fetch"
        ) as mock_second:
            df2 = client.fetch_m5("XAUUSD", start, end)
            assert mock_second.call_count == 0
        # Parquet roundtrip drops the DatetimeIndex.freq attribute, so
        # only assert the values and timestamps are identical.
        pd.testing.assert_frame_equal(df1, df2, check_freq=False)

        # parquet file actually exists on disk
        cached_files = list(
            (tmp_path / "XAUUSD").glob("2024-06_bid.parquet")
        )
        assert len(cached_files) == 1

    def test_partial_cache_only_fetches_missing_months(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        # First populate June only.
        june_fake = _make_fake_df(
            datetime(2024, 6, 1, tzinfo=timezone.utc), n_bars=8640
        )
        with patch(
            "src.data.dukascopy.client.duka.fetch", return_value=june_fake
        ):
            client.fetch_m5(
                "EURUSD",
                start=datetime(2024, 6, 14, tzinfo=timezone.utc),
                end=datetime(2024, 6, 18, tzinfo=timezone.utc),
            )

        # Now request a window spanning May (missing) -> June (cached).
        # Only May's month-call should hit the network.
        may_fake = _make_fake_df(
            datetime(2024, 5, 1, tzinfo=timezone.utc), n_bars=8640
        )
        with patch(
            "src.data.dukascopy.client.duka.fetch", return_value=may_fake
        ) as mock_fetch:
            df = client.fetch_m5(
                "EURUSD",
                start=datetime(2024, 5, 28, tzinfo=timezone.utc),
                end=datetime(2024, 6, 5, tzinfo=timezone.utc),
            )
            assert mock_fetch.call_count == 1
            kwargs = mock_fetch.call_args.kwargs
            # The single call should be the May-2024 month boundary.
            assert kwargs["start"] == datetime(2024, 5, 1, tzinfo=timezone.utc)
            assert kwargs["end"] == datetime(2024, 6, 1, tzinfo=timezone.utc)

        assert len(df) > 0
        # both halves represented in the result
        assert df.index.min() >= datetime(2024, 5, 28, tzinfo=timezone.utc)
        assert df.index.max() < datetime(2024, 6, 5, tzinfo=timezone.utc)

    def test_use_cache_false_bypasses_cache_completely(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        fake = _make_fake_df(
            datetime(2024, 6, 14, tzinfo=timezone.utc), n_bars=288
        )
        with patch(
            "src.data.dukascopy.client.duka.fetch", return_value=fake
        ) as mock_first:
            client.fetch_m5(
                "XAUUSD",
                start=datetime(2024, 6, 14, tzinfo=timezone.utc),
                end=datetime(2024, 6, 15, tzinfo=timezone.utc),
                use_cache=False,
            )
            assert mock_first.call_count == 1
        with patch(
            "src.data.dukascopy.client.duka.fetch", return_value=fake
        ) as mock_second:
            client.fetch_m5(
                "XAUUSD",
                start=datetime(2024, 6, 14, tzinfo=timezone.utc),
                end=datetime(2024, 6, 15, tzinfo=timezone.utc),
                use_cache=False,
            )
            assert mock_second.call_count == 1
        # No parquet should have been written.
        assert not (tmp_path / "XAUUSD").exists()

    def test_cache_dir_none_disables_cache(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=None)
        fake = _make_fake_df(
            datetime(2024, 6, 14, tzinfo=timezone.utc), n_bars=288
        )
        with patch(
            "src.data.dukascopy.client.duka.fetch", return_value=fake
        ) as mock_fetch:
            client.fetch_m5(
                "XAUUSD",
                start=datetime(2024, 6, 14, tzinfo=timezone.utc),
                end=datetime(2024, 6, 15, tzinfo=timezone.utc),
            )
            assert mock_fetch.call_count == 1


# ---------------------------------------------------------------------- #
# Helpers — _months_between
# ---------------------------------------------------------------------- #


class TestMonthsBetween:
    def test_single_month(self):
        assert _months_between(
            datetime(2024, 6, 14, tzinfo=timezone.utc),
            datetime(2024, 6, 18, tzinfo=timezone.utc),
        ) == [(2024, 6)]

    def test_spans_two_months(self):
        assert _months_between(
            datetime(2024, 5, 28, tzinfo=timezone.utc),
            datetime(2024, 6, 5, tzinfo=timezone.utc),
        ) == [(2024, 5), (2024, 6)]

    def test_year_boundary(self):
        assert _months_between(
            datetime(2024, 12, 28, tzinfo=timezone.utc),
            datetime(2025, 1, 5, tzinfo=timezone.utc),
        ) == [(2024, 12), (2025, 1)]

    def test_end_at_month_boundary_excludes_that_month(self):
        # [2024-05-15, 2024-06-01) covers only May.
        assert _months_between(
            datetime(2024, 5, 15, tzinfo=timezone.utc),
            datetime(2024, 6, 1, tzinfo=timezone.utc),
        ) == [(2024, 5)]


# ---------------------------------------------------------------------- #
# Network — real Dukascopy hits, opt-in
# ---------------------------------------------------------------------- #


@pytest.mark.network
class TestNetwork:
    def test_recent_xauusd_returns_bars(self, tmp_path: Path):
        client = DukascopyClient(cache_dir=tmp_path)
        df = client.fetch_m5(
            "XAUUSD",
            start=datetime(2026, 4, 21, tzinfo=timezone.utc),
            end=datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
        assert len(df) > 0
        assert list(df.columns) == CANONICAL_COLUMNS
        assert df.index.tz is not None

    def test_pre_cutoff_eurusd_returns_empty(self, tmp_path: Path):
        # Library cutoff for EURUSD is around 2012-01; 2010 is empty.
        client = DukascopyClient(cache_dir=tmp_path)
        df = client.fetch_m5(
            "EURUSD",
            start=datetime(2010, 6, 14, tzinfo=timezone.utc),
            end=datetime(2010, 6, 19, tzinfo=timezone.utc),
        )
        assert len(df) == 0
        assert list(df.columns) == CANONICAL_COLUMNS
