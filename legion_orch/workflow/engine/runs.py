"""Run lifecycle: failure, timeouts, logs, sandbox deletion, acceptance of reported runs."""
from __future__ import annotations

import itertools
import shutil
from datetime import UTC, datetime

from ...sandbox import agentlog
from ...util import now_iso
from ..model import (
    ACCEPTED,
    BUILD,
    LIVE_RUN,
    R_ACCEPTED,
    R_FAILED,
    R_REPORTED,
    R_SUPERSEDED,
    ROLE_FOR_KIND,
    FailureSource,
    Node,
    Resume,
    Run,
    RunFailure,
)
from ..roles import handler_for
from .common import _parse_ts
from .core import ChangeCore
from .reflect import Reflector


def log_part(run_id: str, n: int) -> str:
    return f"control/logs/{run_id}.log" if n == 1 else f"control/logs/{run_id}.{n}.log"


def split_lines(text: str, limit: int) -> list[str]:
    """``text`` in parts of at most ``limit`` UTF-8 bytes, split after a newline where possible."""
    parts, cur, size = [], [], 0
    for line in text.splitlines(keepends=True):
        b = len(line.encode())
        while b > limit:                         # one huge line: cut it by characters
            head = line[:limit // 4]
            if cur:
                parts.append("".join(cur))
                cur, size = [], 0
            parts.append(head)
            line = line[len(head):]
            b = len(line.encode())
        if size + b > limit and cur:
            parts.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += b
    if cur:
        parts.append("".join(cur))
    return parts or [""]


class RunLifecycle:
    """What happens to runs after dispatch: failure (which escalates – no automatic retries),
    timeouts, the agent log, sandbox deletion once work is accepted."""

    LOG_LIMIT = 64 * 1024 * 1024      # a larger log keeps its first and last 32 MiB
    PART_BYTES = 1024 * 1024         # per stored document (the store's limit is 2 MiB)

    def __init__(self, core: ChangeCore):
        self.c = core
        self.reflector = Reflector(core)

    def live_runs(self) -> list[Run]:
        return [r for r in self.c.runs.values() if r.status in LIVE_RUN]

    def check_timeouts(self) -> None:
        c, limit = self.c, self.c.cfg.limits.run_timeout_s
        now = datetime.now(UTC)
        for run in self.live_runs():
            if (now - _parse_ts(run.created_at)).total_seconds() > limit:
                proc = c.procs.get(run.id)
                if proc and proc.returncode is None:
                    proc.kill()
                self.fail_run(run, f"timed out after {limit}s", FailureSource.TIMEOUT)

    def holds_sandbox(self, node: Node) -> bool:
        """The node's producer still keeps ``node.sandbox`` for rework: roles with
        ``keeps_session_until`` until that gate is accepted, the implementer until its node is.
        Nothing is held once the change is no longer active."""
        if not node.sandbox or node.kind not in ROLE_FOR_KIND or self.c.change.status != "ACTIVE":
            return False
        h = handler_for(ROLE_FOR_KIND[node.kind])
        if h.keeps_session_until is not None:
            gate = self.c.nodes.get(h.keeps_session_until)
            return gate is not None and gate.status != ACCEPTED
        return h.reuses_sandbox and node.status != ACCEPTED

    def _held(self) -> set[str]:
        return {n.sandbox for n in self.c.nodes.values() if self.holds_sandbox(n)}

    def release_sandboxes(self) -> bool:
        """Delete kept sandboxes whose hold has ended (their gate passed, or the change ended)
        once no run uses them. Returns whether anything changed."""
        changed = False
        for node in self.c.nodes.values():
            sb = node.sandbox
            if not sb or self.holds_sandbox(node) or any(
                    r.sandbox == sb and r.status in LIVE_RUN for r in self.c.runs.values()):
                continue
            stopped = node.sandbox_stopped
            node.sandbox, node.sandbox_stopped, changed = None, False, True
            if any(r.sandbox == sb and not r.sandbox_deleted for r in self.c.runs.values()):
                self.c.event("sandbox", f"releasing {sb}: {node.id} is not reworked any more",
                             node.id)
                self.delete_sandbox_later(sb, stopped)
        return changed

    def delete_sandbox_later(self, name: str, stopped: bool = False) -> None:
        """Delete a sandbox in the background (its work is done), after its agent has reflected
        on the session (see :mod:`.reflect`; ``stopped``: the sandbox has to be started first)."""
        self.c.spawn(self._retire(name, stopped))

    async def _retire(self, name: str, stopped: bool) -> None:
        self.reflector.pending.add(name)
        try:
            await self.reflector.reflect(name, stopped)
        finally:
            self.reflector.pending.discard(name)
        await self.delete_sandbox(name)

    def fail_run(self, run: Run, reason: str, source: FailureSource,
                 exit_code: int | None = None) -> None:
        """No automatic retries (principle 5): a failed run escalates to a human. ``source``
        says whether the agent failed or what it runs on (see :class:`FailureSource`)."""
        run.status = R_FAILED
        run.ended_at = now_iso()
        run.failure = RunFailure(source=source, stage=run.stage, reason=reason, exit_code=exit_code)
        node = self.c.nodes.get(run.node_id)
        where = f" while {run.stage.lower()}" if run.stage and run.stage != "EXITED" else ""
        label = f"{source.lower()} error{where}"
        self.c.event("run_failed", f"{label}: {reason}", run.node_id, run.id)
        if node is None:
            return
        tail = self.log_tail(run.id, 40)
        who = f"{run.role} run {run.id}" if handler_for(run.role).panel else f"run {run.id}"
        resume = (Resume.REVIEWING if handler_for(run.role).panel
                  else Resume.REWORK if node.kind == BUILD else Resume.READY)
        self.c.escalate(node, "run_failed", f"{who} ({label}): {reason}", resume=resume,
                        run=run.id, log_tail=tail, source=source)
        if not self.c.cfg.sandbox.keep_failed and run.sandbox and run.role != "implementer":
            # a failed run's sandbox is not kept for rework either: the retry starts afresh
            if node.sandbox == run.sandbox:
                node.sandbox, node.sandbox_stopped = None, False
            self.c.spawn(self.delete_sandbox(run.sandbox))

    def archive_log(self, run: Run) -> None:
        """Move the whole agent log into the store (agents cannot read control/logs/), in parts
        below the store's document size (``<run>.log``, ``<run>.2.log``, …), and record its
        digest. A log over ``LOG_LIMIT`` keeps its first and last halves."""
        f = self.c.run_dir(run.id) / "agent.log"
        if not f.exists():
            return
        data = f.read_bytes()
        if len(data) > self.LOG_LIMIT:
            half = self.LOG_LIMIT // 2
            data = (data[:half] + f"\n[legion: {len(data) - 2 * half} bytes omitted]\n".encode()
                    + data[-half:])
        text = data.decode(errors="replace")
        run.session_id = agentlog.session_id(text) or run.session_id
        for n, part in enumerate(split_lines(text, self.PART_BYTES), start=1):
            self.c.write_record(log_part(run.id, n), part)
        self.c.write_record(f"control/digests/{run.id}.json", agentlog.digest(text))
        shutil.rmtree(self.c.run_dir(run.id), ignore_errors=True)

    def stored_log(self, run_id: str) -> str:
        """A run's whole archived log ('' if none)."""
        parts = []
        for n in itertools.count(1):
            if not self.c.latest(log_part(run_id, n)):
                break
            parts.append(self.c.doc(log_part(run_id, n)).content)
        return "".join(parts)

    def log_tail(self, run_id: str, lines: int = 200) -> str:
        p = self.c.run_dir(run_id) / "agent.log"
        if p.exists():
            text = p.read_bytes()[-200_000:].decode(errors="replace")
        else:
            n = 1
            while self.c.latest(log_part(run_id, n + 1)):
                n += 1
            if not self.c.latest(log_part(run_id, n)):
                return ""
            text = (self.c.doc(log_part(run_id, n - 1)).content if n > 1 else "") \
                + self.c.doc(log_part(run_id, n)).content
        return "\n".join(text.splitlines()[-lines:])

    async def delete_sandbox(self, name: str) -> bool:
        """Delete a sandbox; False (and an event) if OpenShell refused."""
        try:
            await self.c.runner.delete(name)
        except Exception as e:
            self.c.event("sandbox", f"delete of {name} failed: {e}")
            return False
        async with self.c.lock:
            for r in self.c.runs.values():
                if r.sandbox == name:
                    r.sandbox_deleted = True
            self.c.event("sandbox", f"sandbox {name} deleted")
            self.c.save()
        return True

    def accept_runs(self, node: Node, roles: set[str] | None = None) -> None:
        """Mark the node's reported runs ACCEPTED and delete their sandboxes (principle 8),
        except one a node still holds for rework (see :meth:`holds_sandbox`)."""
        sandboxes = set()
        for rid in node.runs:
            r = self.c.runs[rid]
            if roles and r.role not in roles:
                continue
            if r.status == R_REPORTED:
                r.status = R_ACCEPTED
            if r.sandbox and not r.sandbox_deleted and r.status in (R_ACCEPTED, R_SUPERSEDED, R_FAILED):
                sandboxes.add(r.sandbox)
        held = self._held()
        for sb in sandboxes - held:
            if any(r.sandbox == sb and r.status in LIVE_RUN for r in self.c.runs.values()):
                continue
            stopped = False
            for n in self.c.nodes.values():
                if n.sandbox == sb:
                    stopped = stopped or n.sandbox_stopped
                    n.sandbox, n.sandbox_stopped = None, False
            self.delete_sandbox_later(sb, stopped)
