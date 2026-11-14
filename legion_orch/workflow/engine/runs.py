"""Run lifecycle: escalation, failure, logs, sandbox deletion, acceptance.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

import shutil

from ...util import now_iso
from ..model import (
    BUILD,
    ESCALATED,
    LIVE_RUN,
    R_ACCEPTED,
    R_FAILED,
    R_REPORTED,
    R_SUPERSEDED,
    READY,
    REVIEWING,
    REWORK,
    Node,
    Run,
)
from ..roles import handler_for


class RunsMixin:
    def delete_sandbox_later(self, name: str) -> None:
        """Delete a sandbox in the background (its work has been accepted)."""
        self._spawn(self._delete_sandbox(name))

    def escalate(self, node: Node, kind: str, reason: str, resume: str, **extra) -> None:
        node.status = ESCALATED
        node.escalation = {"type": kind, "reason": reason, "resume": resume, "at": now_iso(),
                           **extra}
        self.event("escalation", f"{kind}: {reason}", node.id, extra.get("run"))

    def _fail_run(self, run: Run, reason: str) -> None:
        """No automatic retries (principle 5): a failed run escalates to a human."""
        run.status = R_FAILED
        run.ended_at = now_iso()
        node = self.nodes.get(run.node_id)
        self.event("run_failed", reason, run.node_id, run.id)
        if node is None:
            return
        tail = self.log_tail(run.id, 40)
        if handler_for(run.role).panel:
            self.escalate(node, "run_failed", f"{run.role} run {run.id}: {reason}",
                           resume=REVIEWING, run=run.id, log_tail=tail)
        else:
            resume = REWORK if node.kind == BUILD else READY
            self.escalate(node, "run_failed", f"run {run.id}: {reason}", resume=resume,
                           run=run.id, log_tail=tail)
        if not self.cfg.sandbox.keep_failed and run.sandbox and run.role != "implementer":
            self._spawn(self._delete_sandbox(run.sandbox))

    LOG_LIMIT = 512 * 1024

    def _archive_log(self, run: Run) -> None:
        """Move the agent log into the store (agents cannot read control/logs/)."""
        f = self._run_dir(run.id) / "agent.log"
        if not f.exists():
            return
        data = f.read_bytes()
        text = data[-self.LOG_LIMIT:].decode(errors="replace")
        if len(data) > self.LOG_LIMIT:
            text = f"[truncated: last {self.LOG_LIMIT} of {len(data)} bytes]\n" + text
        self.write_record(f"control/logs/{run.id}.log", text)
        shutil.rmtree(self._run_dir(run.id), ignore_errors=True)

    def log_tail(self, run_id: str, lines: int = 200) -> str:
        p = self._run_dir(run_id) / "agent.log"
        if p.exists():
            text = p.read_bytes()[-200_000:].decode(errors="replace")
        elif self.latest(f"control/logs/{run_id}.log"):
            text = self.doc(f"control/logs/{run_id}.log").content
        else:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    async def _delete_sandbox(self, name: str) -> None:
        try:
            await self.runner.delete(name)
        except Exception as e:
            self.event("sandbox", f"delete of {name} failed: {e}")
            return
        async with self.lock:
            for r in self.runs.values():
                if r.sandbox == name:
                    r.sandbox_deleted = True
            self.event("sandbox", f"sandbox {name} deleted")
            self._save()

    def accept_runs(self, node: Node, roles: set[str] | None = None) -> None:
        """Mark the node's reported runs ACCEPTED and delete their sandboxes (principle 8)."""
        sandboxes = set()
        for rid in node.runs:
            r = self.runs[rid]
            if roles and r.role not in roles:
                continue
            if r.status == R_REPORTED:
                r.status = R_ACCEPTED
            if r.sandbox and not r.sandbox_deleted and r.status in (R_ACCEPTED, R_SUPERSEDED, R_FAILED):
                sandboxes.add(r.sandbox)
        for sb in sandboxes:
            if any(r.sandbox == sb and r.status in LIVE_RUN for r in self.runs.values()):
                continue
            self._spawn(self._delete_sandbox(sb))
