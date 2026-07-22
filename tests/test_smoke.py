"""Trivial smoke test (initial scaffold): the core package imports and exposes a version."""

import endoscan_core


def test_endoscan_core_importable_and_versioned() -> None:
    assert isinstance(endoscan_core.__version__, str)
    assert endoscan_core.__version__
