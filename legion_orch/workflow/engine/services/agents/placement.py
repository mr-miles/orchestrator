"""Where an agent works: which sandbox, which session.

It decides by what it is given – the work's ``SandboxPlan`` (from the ticket) and whether the
work judges another ticket's – never by a role's name:

* a plan that **keeps** (a ticket's own work) reuses the ticket's kept sandbox and resumes its
  session (a rework, a restart: the same attempt again);
* otherwise a **fresh** sandbox (a judge: one per round), and the role's earlier sandboxes on the
  ticket – kept stopped for questions – are retired;
* **G-D4**: a judge never shares the judged work's sandbox (an assertion, not a convention).
"""
from __future__ import annotations

from .....sandbox import AGENT_PREFIX, agentlog
from ....model import LIVE_RUN, Run
from ....tickets import Ticket
from ...base.core import ChangeCore
from ...sandboxes.plan import SandboxPlan
from ...sandboxes.sandboxes import Sandboxes
from ..runs import RunLifecycle


class Placement:
    def __init__(self, core: ChangeCore, sandboxes: Sandboxes, lifecycle: RunLifecycle):
        self.c, self.sandboxes, self.lifecycle = core, sandboxes, lifecycle

    def place(self, run: Run, ticket: Ticket, plan: SandboxPlan, *, judged: Ticket | None) -> None:
        """Choose ``run``'s sandbox (under the lock). ``judged``: the ticket whose work it judges."""
        if plan.keeps and ticket.sandbox:
            run.sandbox = ticket.sandbox
        else:
            run.sandbox = self.c.host.new_sandbox_name(AGENT_PREFIX)
            self.retire_earlier(ticket, run.role, run.sandbox)
        run.sandbox_ref = next((r.sandbox_ref for r in self.c.runs.values()
                                if r.sandbox == run.sandbox and r.sandbox_ref), None)
        if judged is not None and (judged.sandbox == run.sandbox or any(
                self.c.runs[r].sandbox == run.sandbox for r in judged.runs if r in self.c.runs)):
            raise RuntimeError("G-D4 violated: a judge would share a sandbox with the judged work")

    def retire_earlier(self, ticket: Ticket, role: str, keep: str) -> None:
        """A new sandbox for the role (a judge's next round): the role's earlier ones on the
        ticket – kept stopped for questions – are retired (once nothing live uses them)."""
        for sb in {r.sandbox for r in self.c.runs.values()
                   if r.ticket == ticket.id and r.role == role and r.sandbox != keep
                   and r.sandbox and not r.sandbox_deleted and r.sandbox != ticket.sandbox}:
            if sb not in self.sandboxes.retiring and not any(
                    x.sandbox == sb and x.status in LIVE_RUN for x in self.c.runs.values()):
                self.sandboxes.delete_later(sb)

    def resumable_session(self, run: Run, plan: SandboxPlan) -> tuple[str, str] | None:
        """The session ``run`` continues – a plan keeping its session, or a consult answering in
        place: the ticket's previous run of the role in its sandbox (a consult's: its previous
        consult too – the resolver's turns are all consults; a round's work: never a consult)."""
        if not (plan.keeps_session or run.consult):
            return None
        prev = [r for r in self.c.runs.values() if r.ticket == run.ticket and r.role == run.role
                and r.sandbox == run.sandbox and r.id != run.id and (run.consult or not r.consult)]
        if not prev:
            return None
        sid = prev[-1].session_id or agentlog.session_id(self.lifecycle.log_tail(prev[-1].id, 100_000))
        return (prev[-1].id, sid) if sid else None
