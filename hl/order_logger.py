"""Append every order attempt to a JSONL log under logs/hl-execution/.

One line per attempt with request, response, and latency. Readable via
`jq` and easy to grep for a cloid. Logs live inside this repo's `logs/`
directory (gitignored at the repo root as `logs/`) so operational data
never leaves the project tree.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

# hl/order_logger.py → hl/ → repo root → logs/hl-execution/
_LOG_ROOT = Path(__file__).resolve().parent.parent / "logs" / "hl-execution"


def _log_path() -> Path:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    _LOG_ROOT.mkdir(parents=True, exist_ok=True)
    return _LOG_ROOT / f"{day}.jsonl"


def log_attempt(action: str, request: dict, response: dict | None, latency_ms: float, error: str | None = None) -> None:
    """Write one log line. `action` is e.g. 'order', 'cancel', 'set-leverage'."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "action": action,
        "request": request,
        "response": response,
        "error": error,
        "latency_ms": round(latency_ms, 2),
        "pid": os.getpid(),
    }
    with _log_path().open("a") as f:
        f.write(json.dumps(entry, default=str))
        f.write("\n")


class Timer:
    """Context manager for latency measurement.

    Use `t.elapsed` to read latency at any point (including inside an except
    block). `t.elapsed_ms` is set on context exit and stays stable afterwards.
    """

    def __enter__(self):
        self.start = time.monotonic()
        self.elapsed_ms = 0.0
        return self

    def __exit__(self, *_):
        self.elapsed_ms = self.elapsed

    @property
    def elapsed(self) -> float:
        return (time.monotonic() - self.start) * 1000
