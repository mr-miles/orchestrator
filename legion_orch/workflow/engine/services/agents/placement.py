"""Where an agent works: which sandbox, which session (~/plans/inbox-runner.md, placement).

It decides by what it is given – the work's ``SandboxPlan`` (from the step type) and whether the
work judges another run – never by a role's name:

* a plan that **keeps** (a producer: ``UntilGate``, ``UntilAccepted``) reuses the node's kept
  sandbox and, keeping its session, resumes it;
* an agent **started early** (a consult, to answer a message) is adopted: its run continues
  that sandbox and session;
* otherwise a **fresh** sandbox (a judge: one per work, so per cycle), and the role's earlier
  sandboxes on the node – kept stopped for questions – are retired;
* **G-D4**: a judge never shares the judged run's sandbox (an assertion, not a convention).
"""
from __future__ import annotations

from .....sandbox import AGENT_PREFIX, agentlog
from ....model import LIVE_RUN, Node, Run
from ...base.core import ChangeCore
from ...sandboxes.plan import SandboxPlan
from ...sandboxes.sandboxes import Sandboxes
from ..runs import RunLifecycle


class Placement:
    def __init__(self, core: ChangeCore, sandboxes: Sandboxes, lifecycle: RunLifecycle):
        self.c, self.sandboxes, self.lifecycle = core, sandboxes, lifecycle

    def place(self, run: Run, node: Node, plan: SandboxPlan, *, judge: bool) -> None:
        """Choose ``run``'s sandbox (under the lock)."""
        early = self.consulted(node, run.role)
        if plan.keeps and node.sandbox:
            run.sandbox = node.sandbox
        elif early is not None:        # started early to answer a message (A2A): its agent
            run.sandbox = early.sandbox
        else:
            run.sandbox = self.c.host.new_sandbox_name(AGENT_PREFIX)
            self.retire_earlier(node, run.role, run.sandbox)
        run.sandbox_ref = next((r.sandbox_ref for r in self.c.runs.values()
                                if r.sandbox == run.sandbox and r.sandbox_ref), None)
        if judge and node.current_run:
            judged = self.c.runs.get(node.current_run)
            if judged and (judged.sandbox == run.sandbox or judged.id == run.id):
                raise RuntimeError("G-D4 violated: judge would share a sandbox with judged run")

    def consulted(self, node: Node, role: str) -> Run | None:
        """The role's agent started early on this node (a consult) whose sandbox its run
        continues: the latest consult of the role, unless a run of the role came since."""
        runs = [r for r in self.c.runs.values() if r.node_id == node.id and r.role == role
                and r.sandbox and not r.sandbox_deleted]
        return runs[-1] if runs and runs[-1].consult else None

    def retire_earlier(self, node: Node, role: str, keep: str) -> None:
        """A new sandbox for the role (a judge's next cycle): the role's earlier ones on the
        node – kept stopped for questions – are retired (once nothing live uses them)."""
        for sb in {r.sandbox for r in self.c.runs.values()
                   if r.node_id == node.id and r.role == role and r.sandbox != keep
                   and r.sandbox and not r.sandbox_deleted and r.sandbox != node.sandbox}:
            if sb not in self.sandboxes.retiring and not any(
                    x.sandbox == sb and x.status in LIVE_RUN for x in self.c.runs.values()):
                self.sandboxes.delete_later(sb)

    def resumable_session(self, run: Run, plan: SandboxPlan) -> tuple[str, str] | None:
        """The session ``run`` continues: an agent started early in its sandbox, or – a plan
        keeping its session – the node's previous run of the role there."""
        consults = [r for r in self.c.runs.values() if r.consult and r.node_id == run.node_id
                    and r.role == run.role and r.sandbox == run.sandbox and r.id != run.id]
        node = self.c.nodes.get(run.node_id)
        prev = [self.c.runs[r] for r in node.runs if r != run.id and r in self.c.runs] if node else []
        prev = [r for r in prev if r.role == run.role and r.sandbox == run.sandbox]
        if consults and (not prev or consults[-1].created_at >= prev[-1].created_at):
            prev = consults                  # it continues the agent started early (A2A)
        elif not plan.keeps_session:
            return None
        if not prev:
            return None
        sid = prev[-1].session_id or agentlog.session_id(self.lifecycle.log_tail(prev[-1].id, 100_000))
        return (prev[-1].id, sid) if sid else None
