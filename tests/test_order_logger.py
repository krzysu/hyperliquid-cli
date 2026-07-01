"""Tests for hl/order_logger.py — Timer.elapsed must be readable mid-flight,
inside an except block, so failure-path latency logging is accurate."""

from __future__ import annotations

import time

from hl.order_logger import Timer


def test_elapsed_readable_mid_block():
    with Timer() as t:
        time.sleep(0.01)
        mid = t.elapsed
    assert mid >= 9  # at least ~10ms
    assert t.elapsed_ms >= mid


def test_elapsed_in_except():
    """Simulate the cmd_order error path: read t.elapsed inside except, before fail()."""
    captured = None
    try:
        with Timer() as t:
            time.sleep(0.005)
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                captured = t.elapsed
                raise
    except RuntimeError:
        pass
    assert captured is not None
    assert captured >= 4  # at least ~5ms — proves it's not the bug-shape 0


def test_elapsed_ms_set_on_clean_exit():
    with Timer() as t:
        pass
    assert t.elapsed_ms >= 0
