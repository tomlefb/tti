"""Trivial smoke test: proves pytest is wired and the src package imports."""

import importlib


def test_src_package_importable() -> None:
    """The top-level src package must import without side effects."""
    module = importlib.import_module("src")
    assert module is not None
