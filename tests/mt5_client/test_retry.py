"""Unit tests for src.mt5_client.retry."""

from __future__ import annotations

import pytest

from src.mt5_client.retry import with_retry


def test_succeeds_on_first_try():
    calls = {"n": 0}

    def ok():
        calls["n"] += 1
        return 42

    sleeps: list[float] = []
    out = with_retry(ok, max_attempts=3, base_delay=0.1, sleep=sleeps.append)
    assert out == 42
    assert calls["n"] == 1
    assert sleeps == []


def test_retries_on_failure_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"transient {calls['n']}")
        return "ok"

    sleeps: list[float] = []
    out = with_retry(flaky, max_attempts=5, base_delay=1.0, sleep=sleeps.append)
    assert out == "ok"
    assert calls["n"] == 3
    # Two sleeps before the third attempt: 1.0, 2.0.
    assert sleeps == [1.0, 2.0]


def test_gives_up_after_max_attempts():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RuntimeError(f"boom {calls['n']}")

    sleeps: list[float] = []
    with pytest.raises(RuntimeError, match="boom 5"):
        with_retry(always_fails, max_attempts=5, base_delay=0.5, sleep=sleeps.append)
    assert calls["n"] == 5
    # 4 retries before final failure: 0.5, 1.0, 2.0, 4.0.
    assert sleeps == [0.5, 1.0, 2.0, 4.0]


def test_rejects_zero_max_attempts():
    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        with_retry(lambda: None, max_attempts=0)
