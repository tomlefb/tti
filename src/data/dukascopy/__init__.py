"""Dukascopy historical data client.

Wraps ``dukascopy_python`` (PyPI v4.0.1) to expose a normalised, cached
interface for fetching M5 OHLCV bars on the project's seven canonical
instruments. See ``src/data/dukascopy/client.py``.
"""

from src.data.dukascopy.client import (
    CANONICAL_COLUMNS,
    DEFAULT_CACHE_DIR,
    INSTRUMENT_MAPPING,
    DukascopyClient,
    canonical_instruments,
    from_dukascopy_code,
    to_dukascopy_code,
)

__all__ = [
    "CANONICAL_COLUMNS",
    "DEFAULT_CACHE_DIR",
    "INSTRUMENT_MAPPING",
    "DukascopyClient",
    "canonical_instruments",
    "from_dukascopy_code",
    "to_dukascopy_code",
]
