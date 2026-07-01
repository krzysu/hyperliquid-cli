"""Auto-applied test fixtures.

Redirects the order-attempt log to a per-test temp directory so the suite
never writes to the real `logs/hl-execution/` tree. Without this,
any test that exercises a code path calling `log_attempt` (e.g.
`_cleanup_orphan_legs`) would pollute the operational log file.
"""

from __future__ import annotations

import pytest

from hl import order_logger


@pytest.fixture(autouse=True)
def _isolate_order_log(tmp_path, monkeypatch):
    monkeypatch.setattr(order_logger, "_LOG_ROOT", tmp_path / "orders")
