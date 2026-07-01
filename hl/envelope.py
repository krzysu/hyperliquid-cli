"""JSON envelope + error categorization for the hl CLI.

Success: print the SDK's JSON payload verbatim to stdout, exit 0.
Failure: print `{"ok": false, "error": <category>, "message": <str>}`, exit 1.

Categories: api, network, rate_limit, validation, auth, config,
io, parse, execution_error (HL rejected the order), preflight_error
(our local safety guard blocked it).
"""

from __future__ import annotations

import json
import sys
from typing import Any, NoReturn


def emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, default=str)
    sys.stdout.write("\n")


def fail(category: str, message: str) -> NoReturn:
    emit({"ok": False, "error": category, "message": message})
    sys.exit(1)


def categorize(exc: BaseException) -> tuple[str, str]:
    """Map an exception into (category, message) for the error envelope."""
    import requests

    if isinstance(exc, requests.HTTPError):
        resp = exc.response
        if resp is not None:
            if resp.status_code == 429:
                return "rate_limit", f"HTTP 429: {resp.text[:200]}"
            if resp.status_code in (401, 403):
                return "auth", f"HTTP {resp.status_code}: {resp.text[:200]}"
            if 400 <= resp.status_code < 500:
                return "validation", f"HTTP {resp.status_code}: {resp.text[:200]}"
            return "api", f"HTTP {resp.status_code}: {resp.text[:200]}"
        return "api", str(exc)
    if isinstance(exc, requests.Timeout):
        return "network", f"timeout: {exc}"
    if isinstance(exc, requests.ConnectionError):
        return "network", f"connection error: {exc}"
    if isinstance(exc, ValueError):
        return "validation", str(exc)
    return "api", f"{type(exc).__name__}: {exc}"
