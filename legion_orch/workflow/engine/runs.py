"""Run lifecycle: failure, timeouts, logs, sandbox deletion, acceptance of reported runs."""
from __future__ import annotations

import shutil
from datetime import UTC, datetime

from ...util import now_iso
from ..model import (
    BUILD,
    LIVE_RUN,
    R_ACCEPTED,
    R_FAILED,
    R_REPORTED,
    R_SUPERSEDED,
    Node,
    Resume,
    Run,
)
from ..roles import handler_for
from .common import _parse_ts
from .core import ChangeCore


class RunLifecycle:
    """What happens to runs after dispatch: failure (which escalates – no automatic retries),
    timeouts, the agent log, sandbox deletion once work is accepted."""

    LOG_LIMIT = 512 * 1024

    def __init__(self, core: ChangeCore):
        self.c = core

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
                self.fail_run(run, f"timed out after {limit}s")

    def delete_sandbox_later(self, name: str) -> None:
        """Delete a sandbox in the background (its work has been accepted)."""
        self.c.spawn(self.delete_sandbox(name))

    def fail_run(self, run: Run, reason: str) -> None:
        """No automatic retries (principle 5): a failed run escalates to a human."""
        run.status = R_FAILED
        run.ended_at = now_iso()
        node = self.c.nodes.get(run.node_id)
        self.c.event("run_failed", reason, run.node_id, run.id)
        if node is None:
            return
        tail = self.log_tail(run.id, 40)
        if handler_for(run.role).panel:
            self.c.escalate(node, "run_failed", f"{run.role} run {run.id}: {reason}",
                            resume=Resume.REVIEWING, run=run.id, log_tail=tail)
        else:
            resume = Resume.REWORK if node.kind == BUILD else Resume.READY
            self.c.escalate(node, "run_failed", f"run {run.id}: {reason}", resume=resume,
                            run=run.id, log_tail=tail)
        if not self.c.cfg.sandbox.keep_failed and run.sandbox and run.role != "implementer":
            self.c.spawn(self.delete_sandbox(run.sandbox))

    def archive_log(self, run: Run) -> None:
        """Move the agent log into the store (agents cannot read control/logs/)."""
        f = self.c.run_dir(run.id) / "agent.log"
        if not f.exists():
            return
        data = f.read_bytes()
        text = data[-self.LOG_LIMIT:].decode(errors="replace")
        if len(data) > self.LOG_LIMIT:
            text = f"[truncated: last {self.LOG_LIMIT} of {len(data)} bytes]\n" + text
        self.c.write_record(f"control/logs/{run.id}.log", text)
        shutil.rmtree(self.c.run_dir(run.id), ignore_errors=True)

    def log_tail(self, run_id: str, lines: int = 200) -> str:
        p = self.c.run_dir(run_id) / "agent.log"
        if p.exists():
            text = p.read_bytes()[-200_000:].decode(errors="replace")
        elif self.c.latest(f"control/logs/{run_id}.log"):
            text = self.c.doc(f"control/logs/{run_id}.log").content
        else:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    async def delete_sandbox(self, name: str) -> None:
        try:
            await self.c.runner.delete(name)
        except Exception as e:
            self.c.event("sandbox", f"delete of {name} failed: {e}")
            return
        async with self.c.lock:
            for r in self.c.runs.values():
                if r.sandbox == name:
                    r.sandbox_deleted = True
            self.c.event("sandbox", f"sandbox {name} deleted")
            self.c.save()

    def accept_runs(self, node: Node, roles: set[str] | None = None) -> None:
        """Mark the node's reported runs ACCEPTED and delete their sandboxes (principle 8)."""
        sandboxes = set()
        for rid in node.runs:
            r = self.c.runs[rid]
            if roles and r.role not in roles:
                continue
            if r.status == R_REPORTED:
                r.status = R_ACCEPTED
            if r.sandbox and not r.sandbox_deleted and r.status in (R_ACCEPTED, R_SUPERSEDED, R_FAILED):
                sandboxes.add(r.sandbox)
        for sb in sandboxes:
            if any(r.sandbox == sb and r.status in LIVE_RUN for r in self.c.runs.values()):
                continue
            self.c.spawn(self.delete_sandbox(sb))
