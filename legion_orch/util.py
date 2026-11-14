"""Small shared helpers with no orchestrator dependencies."""
from __future__ import annotations

import base64
from datetime import datetime, timezone


def now_iso() -> str:
    """Canonical UTC timestamp, YYYY-MM-DDTHH:MM:SS.mmmZ."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def bearer(header: str) -> str | None:
    """Extract a bearer (or Basic password) from an Authorization header."""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    if header.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            return pw or user or None
        except Exception:
            return None
    return None
