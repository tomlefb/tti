"""Normalised, cached client around ``dukascopy_python``.

Exposes a single class :class:`DukascopyClient` that:

1. Translates the project's canonical instrument names (``XAUUSD``,
   ``NDX100``, ``SPX500``, ``EURUSD``, ``GBPUSD``, ``US30``, ``BTCUSD``)
   to the Dukascopy library codes via :data:`INSTRUMENT_MAPPING`.
2. Fetches M5 OHLCV bars over an arbitrary UTC window via
   :meth:`DukascopyClient.fetch_m5`.
3. Optionally caches results on disk as one parquet file per
   ``(instrument, year-month, side)`` so subsequent calibration runs
   can be served without hitting the network.

Limits inherited from the underlying library — see
``calibration/dukascopy_coverage_check_2026-05-02T14-28-29Z.md``:

* FX majors and US indices are served from approximately 2012-01 onwards.
* BTCUSD is served from approximately 2017-06 onwards.
* XAUUSD reaches back to at least 2008.

Pre-cutoff windows return an empty :class:`pandas.DataFrame` with the
canonical schema rather than raising.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import dukascopy_python as duka
import pandas as pd
from dukascopy_python import instruments as duka_instruments

logger = logging.getLogger(__name__)

# Canonical project name -> Dukascopy library code.
INSTRUMENT_MAPPING: dict[str, str] = {
    "XAUUSD": duka_instruments.INSTRUMENT_FX_METALS_XAU_USD,
    "NDX100": duka_instruments.INSTRUMENT_IDX_AMERICA_E_NQ_100,
    "SPX500": duka_instruments.INSTRUMENT_IDX_AMERICA_E_SANDP_500,
    "EURUSD": duka_instruments.INSTRUMENT_FX_MAJORS_EUR_USD,
    "GBPUSD": duka_instruments.INSTRUMENT_FX_MAJORS_GBP_USD,
    "US30": duka_instruments.INSTRUMENT_IDX_AMERICA_E_D_J_IND,
    "BTCUSD": duka_instruments.INSTRUMENT_VCCY_BTC_USD,
}

DEFAULT_CACHE_DIR: Path = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "dukascopy"
)

CANONICAL_COLUMNS: list[str] = ["open", "high", "low", "close", "volume"]

_DEFAULT_SENTINEL = object()


def canonical_instruments() -> list[str]:
    """Return the sorted list of canonical instrument names supported."""
    return sorted(INSTRUMENT_MAPPING.keys())


def to_dukascopy_code(instrument: str) -> str:
    """Translate a canonical project name to its Dukascopy library code.

    Args:
        instrument: Canonical project name (e.g. ``"XAUUSD"``).

    Returns:
        The Dukascopy library code (e.g. ``"XAU/USD"``).

    Raises:
        ValueError: If ``instrument`` is not a known canonical name.
    """
    try:
        return INSTRUMENT_MAPPING[instrument]
    except KeyError as exc:
        raise ValueError(
            f"Unknown instrument {instrument!r}. "
            f"Supported: {canonical_instruments()}"
        ) from exc


def from_dukascopy_code(code: str) -> str:
    """Translate a Dukascopy library code back to the canonical name.

    Args:
        code: Dukascopy library code (e.g. ``"XAU/USD"``).

    Returns:
        Canonical project name (e.g. ``"XAUUSD"``).

    Raises:
        ValueError: If ``code`` is not a known Dukascopy code in the
            project's mapping.
    """
    inverse = {v: k for k, v in INSTRUMENT_MAPPING.items()}
    try:
        return inverse[code]
    except KeyError as exc:
        raise ValueError(
            f"Unknown Dukascopy code {code!r}. "
            f"Supported: {sorted(inverse.keys())}"
        ) from exc


class DukascopyClient:
    """Normalised, cached client for Dukascopy M5 OHLCV.

    Wraps :func:`dukascopy_python.fetch` and manages an on-disk cache of
    monthly parquet files. The cache is granular per
    ``(instrument, year-month, side)`` so partial re-fetches only touch
    the months that are missing.
    """

    def __init__(self, cache_dir: Path | None = _DEFAULT_SENTINEL) -> None:  # type: ignore[assignment]
        """Initialise the client.

        Args:
            cache_dir: Directory under which to store the monthly parquet
                cache. If omitted, defaults to
                :data:`DEFAULT_CACHE_DIR` (``tests/fixtures/dukascopy``
                relative to the project root). Pass ``None`` to disable
                on-disk caching entirely; in that case ``use_cache`` is
                forced to ``False`` regardless of the per-call flag.
        """
        if cache_dir is _DEFAULT_SENTINEL:
            cache_dir = DEFAULT_CACHE_DIR
        if cache_dir is None:
            self.cache_dir: Path | None = None
        else:
            self.cache_dir = Path(cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch_m5(
        self,
        instrument: str,
        start: datetime,
        end: datetime,
        side: Literal["bid", "ask"] = "bid",
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Fetch M5 OHLCV bars on the half-open window ``[start, end)``.

        Naive datetimes are interpreted as UTC. The returned DataFrame is
        sliced to ``start <= idx < end`` so cached and direct paths are
        bit-for-bit identical for the same arguments.

        When ``use_cache`` is ``True`` and the client has a ``cache_dir``,
        the window is split into calendar months. Each missing month is
        fetched in its entirety from the network and persisted as a
        parquet file; cached months are read from disk and never re-hit
        the network.

        Args:
            instrument: Canonical project name (e.g. ``"XAUUSD"``).
            start: Inclusive lower bound. Naive => UTC.
            end: Exclusive upper bound. Naive => UTC.
            side: ``"bid"`` (default) or ``"ask"``.
            use_cache: Consult and update the on-disk cache. Forced to
                ``False`` when the client was constructed without a
                cache directory.

        Returns:
            DataFrame indexed by tz-aware UTC :class:`pandas.DatetimeIndex`
            (M5 cadence, weekends/holidays naturally absent), with columns
            ``open``, ``high``, ``low``, ``close``, ``volume``. Empty
            DataFrame with the canonical schema if no bars are available
            (including pre-cutoff windows).

        Raises:
            ValueError: If ``instrument`` is unknown, ``start >= end``,
                or ``side`` is not ``"bid"`` / ``"ask"``.
            Exception: Network errors from the underlying library are
                propagated unchanged.
        """
        if start >= end:
            raise ValueError(f"start must be < end (got start={start}, end={end})")
        if side not in ("bid", "ask"):
            raise ValueError(f"side must be 'bid' or 'ask' (got {side!r})")
        code = to_dukascopy_code(instrument)
        start_utc = _ensure_utc(start)
        end_utc = _ensure_utc(end)

        if not use_cache or self.cache_dir is None:
            df = self._fetch_from_network(code, start_utc, end_utc, side)
            return _filter_window(df, start_utc, end_utc)

        frames: list[pd.DataFrame] = []
        for year, month in _months_between(start_utc, end_utc):
            month_df = self._load_month(instrument, year, month, side)
            if month_df is None:
                logger.info(
                    "dukascopy cache miss: %s %04d-%02d (%s) — fetching",
                    instrument, year, month, side,
                )
                month_df = self._fetch_month(code, year, month, side)
                self._save_month(instrument, year, month, side, month_df)
            frames.append(month_df)

        if not frames:
            return _empty_frame()
        full = pd.concat(frames).sort_index()
        full = full[~full.index.duplicated(keep="last")]
        return _filter_window(full, start_utc, end_utc)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _fetch_from_network(
        self,
        code: str,
        start: datetime,
        end: datetime,
        side: str,
    ) -> pd.DataFrame:
        offer_side = (
            duka.OFFER_SIDE_BID if side == "bid" else duka.OFFER_SIDE_ASK
        )
        df = duka.fetch(
            instrument=code,
            interval=duka.INTERVAL_MIN_5,
            offer_side=offer_side,
            start=start,
            end=end,
        )
        return _normalise(df)

    def _fetch_month(
        self,
        code: str,
        year: int,
        month: int,
        side: str,
    ) -> pd.DataFrame:
        month_start = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            month_end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        return self._fetch_from_network(code, month_start, month_end, side)

    def _cache_path(
        self, instrument: str, year: int, month: int, side: str
    ) -> Path:
        if self.cache_dir is None:
            raise RuntimeError("cache_dir is None; cache path requested anyway")
        return (
            self.cache_dir
            / instrument
            / f"{year:04d}-{month:02d}_{side}.parquet"
        )

    def _load_month(
        self, instrument: str, year: int, month: int, side: str
    ) -> pd.DataFrame | None:
        path = self._cache_path(instrument, year, month, side)
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        return _normalise(df)

    def _save_month(
        self,
        instrument: str,
        year: int,
        month: int,
        side: str,
        df: pd.DataFrame,
    ) -> None:
        path = self._cache_path(instrument, year, month, side)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to a per-process tmp path then rename.
        # ``os.replace`` is atomic on POSIX and Windows for same-volume
        # renames, which gives us multi-process write safety without
        # platform-specific locking.
        tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        df.to_parquet(tmp_path)
        os.replace(tmp_path, path)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a tz-aware UTC datetime (naive => UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _empty_frame() -> pd.DataFrame:
    """Return an empty DataFrame with the canonical schema."""
    df = pd.DataFrame(
        {col: pd.Series(dtype="float64") for col in CANONICAL_COLUMNS}
    )
    df.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
    return df


def _normalise(df: pd.DataFrame | None) -> pd.DataFrame:
    """Coerce the lib's DataFrame into the project's canonical schema."""
    if df is None or len(df) == 0:
        return _empty_frame()
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    for col in CANONICAL_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0 if col == "volume" else float("nan")
    df = df[CANONICAL_COLUMNS]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df.index.name = "timestamp"
    return df.sort_index()


def _filter_window(
    df: pd.DataFrame, start: datetime, end: datetime
) -> pd.DataFrame:
    if len(df) == 0:
        return df
    return df[(df.index >= start) & (df.index < end)]


def _months_between(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """List of ``(year, month)`` pairs covered by ``[start, end)``."""
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    last_y, last_m = end.year, end.month
    # If end is exactly at a month boundary, exclude that month.
    if end.day == 1 and end.hour == 0 and end.minute == 0 and end.second == 0 and end.microsecond == 0:
        # walk one step back
        if last_m == 1:
            last_y -= 1
            last_m = 12
        else:
            last_m -= 1
    while (y, m) <= (last_y, last_m):
        months.append((y, m))
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return months
