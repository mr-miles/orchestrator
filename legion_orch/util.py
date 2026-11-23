"""Small shared helpers with no orchestrator dependencies."""
from __future__ import annotations

import asyncio
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


async def run_command(argv, timeout: float, *, env: dict | None = None,
                      cwd: str | None = None) -> tuple[int, str]:
    """Run ``argv`` (no stdin; stderr into stdout) and return its exit code and output. Raises
    ``TimeoutError`` after ``timeout`` seconds; the process never outlives the call – timed out,
    failed or cancelled (:func:`stop_process`)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env, cwd=cwd)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:                 # timed out or cancelled: do not leave it running
        await stop_process(proc)
        raise
    return proc.returncode, out.decode(errors="replace")
