"""Custom exceptions for the MT5 client.

The detection cycle catches ``MT5Error`` (and its subclasses) to skip a
pair without crashing the scheduler. ``MT5ConnectionError`` is special —
it is also raised by ``MT5Client.connect()``, where the caller is the
scheduler bootstrap and the appropriate response is to abort startup.
"""

from __future__ import annotations


class MT5Error(Exception):
    """Base class for every MT5-specific failure raised by this package."""


class MT5ConnectionError(MT5Error):
    """Raised when ``mt5.initialize()`` fails or the terminal is unreachable."""


class MT5DataError(MT5Error):
    """Raised when an OHLC fetch returns no data or malformed data."""


class MT5AccountError(MT5Error):
    """Raised when ``mt5.account_info()`` returns ``None`` or unparseable."""
