"""Small shared helpers with no orchestrator dependencies."""
from __future__ import annotations

import base64
import contextlib
from datetime import UTC, datetime
from typing import Any


def now_iso() -> str:
    """Canonical UTC timestamp, YYYY-MM-DDTHH:MM:SS.mmmZ."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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


async def stop_process(proc: Any) -> None:
    """Kill ``proc`` (an asyncio process, or anything with ``returncode``, ``kill`` and ``wait``)
    if it is still running, and wait for it: a child must not outlive the task that started it,
    whether that task timed out, failed or was cancelled."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()
