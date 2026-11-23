"""The executors' ports, filled by the engine's services (plan: ~/plans/tickets-3b.md, E4).

* :class:`Agents` – ``agent:<role>``: a run of the role through the agent runner (it places the
  run, prepares the working copy and runs the agent's turns); the run's end, read by the report
  service and the run lifecycle, is the :class:`.AgentEnd`.
* :class:`Signoffs` – ``human:signoff``: the subjects shown to the decision owners; a decision
  owner's decision (:meth:`Signoffs.decide`, the API) ends it; recorded as the ticket's
  ``signoff.json``.
* :class:`Verify` – ``controller:verify``: the verification service, at the judged commit.
* :class:`Provision` – ``controller:provision``: the provisioner.
* :class:`Remote` – ``controller:publish`` with a remote: the publisher (GitHub).
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ....util import now_iso
from ...model import FailureSource, Verification
from ...tickets import Status, Ticket
from ...tickets.executors import AgentEnd, Decision, Work
from ..base.common import WorkflowError
from ..base.core import ChangeCore
from ..base.dispatch import Dispatcher
from ..sandboxes.plan import plan_for
from ..services.agent_runs import AgentRuns, ticket_dir
from ..services.agents import AgentRunner
from ..services.provision import Provisioner
from ..services.publish import LabelSpec, Publisher
from ..services.runs import RunLifecycle
from ..services.verification import STEP, Verifications, record_path


class Dispatch:
    """The shared dispatcher, as a ticket's slot (no reservation: tickets queue as they come)."""

    def __init__(self, dispatcher: Dispatcher):
        self.dispatcher = dispatcher

    async def run(self, work):
        return await self.dispatcher.run(work)


def live(c: ChangeCore, work: Work) -> Ticket | None:
    """The ticket ``work`` is a round of, while that round is still on (its outcome is wanted)."""
    t = c.tickets.t.get(work.ticket.id)
    return t if t is not None and t.status == Status.WORKING and t.attempt == work.attempt \
        else None


class Agents:
    """``agent:<role>`` tickets: one run of the role per round."""

    def __init__(self, c: ChangeCore, runner: AgentRunner, agents: AgentRuns,
                 lifecycle: RunLifecycle, dispatcher: Dispatcher):
        self.c, self.runner, self.agents, self.lifecycle = c, runner, agents, lifecycle
        self.dispatch = Dispatch(dispatcher)

    async def run(self, role: str, work: Work) -> AgentEnd:
        c = self.c
        async with c.lock:
            ticket = live(c, work)
            if ticket is None:
                return AgentEnd(False, failure=FailureSource.CONTROLLER, detail="not wanted")
            plan = plan_for(ticket, c.assets.roles[role].git)
            self.runner.open_ticket(ticket)
        outcome, prepared = await self.runner.publish_work(
            ticket, role, work, plan, wanted=lambda: live(c, work) is not None,
            started=lambda run: None, dispatch=self.dispatch)
        async with c.lock:
            if prepared.refused is not None:           # its prompt does not render: the assets
                return AgentEnd(False, failure=FailureSource.CONTROLLER,
                                detail=f"the run could not be prepared: {prepared.refused}")
            run = prepared.run
            if run is None:
                return AgentEnd(False, failure=FailureSource.SYSTEM,
                                detail=outcome.error or "the run never started")
            end = await self.agents.conclude(run)
            run.concluded = True
            if end is None:
                end = AgentEnd(False, failure=FailureSource.SYSTEM,
                               detail="the agent's turns ended without an outcome")
            elif end.accepted:
                self.lifecycle.accept_runs(ticket)
            c.save()
            return end

    async def stop(self, ticket: str) -> None:
        async with self.c.lock:
            for run in self.c.runs.values():
                if run.ticket == ticket and (proc := self.c.procs.get(run.id)) is not None \
                        and proc.returncode is None:
                    proc.kill()


class Signoffs:
    """``human:signoff`` tickets: waits for a decision owner. The subjects are what the work it
    checks produced (pinned): the API shows them; :meth:`decide` ends the wait."""

    def __init__(self, c: ChangeCore):
        self.c = c
        self.waiting: dict[str, tuple[Work, dict[str, Any], asyncio.Future]] = {}

    async def decide(self, work: Work, subjects: Mapping[str, Any]) -> Decision:
        fut = asyncio.get_running_loop().create_future()
        async with self.c.lock:
            self.waiting[work.ticket.id] = (work, dict(subjects), fut)
            self.c.event("signoff", f"waiting for a decision owner on "
                         f"{', '.join(subjects) or 'nothing'}", work.ticket.id)
            self.c.save()
        try:
            return await fut
        finally:
            self.waiting.pop(work.ticket.id, None)

    def answer(self, ticket_id: str, approved: bool, approver: str, reason: str = "") -> None:
        """A decision owner's decision (the caller holds the lock and checked the approver):
        recorded with exactly what was shown, then the sign-off goes on."""
        if ticket_id not in self.waiting:
            raise WorkflowError(f"{ticket_id} is not waiting for a sign-off")
        if not approved and not reason.strip():
            raise WorkflowError("a rejection needs a reason")
        work, subjects, fut = self.waiting[ticket_id]
        self.c.write_record(f"{ticket_dir(ticket_id)}/signoff.json", {
            "ticket": ticket_id, "attempt": work.attempt, "decision":
            "approved" if approved else "rejected", "approver": approver, "at": now_iso(),
            "reason": reason, "subjects": subjects})
        self.c.event("signoff", f"{approver} {'approved' if approved else 'rejected'}"
                     + (f": {reason}" if reason else ""), ticket_id)
        if not fut.done():
            fut.set_result(Decision(approved, reason))

    async def stop(self, ticket: str) -> None:
        if (w := self.waiting.get(ticket)) and not w[2].done():
            w[2].cancel()


class Verify:
    """``controller:verify``: the commands of the work it checks – its owner's own (a build's
    ``verification``), else the approved oracle's – at the judged commit."""

    def __init__(self, c: ChangeCore, verifications: Verifications, dispatcher: Dispatcher):
        self.c, self.svc, self.dispatch = c, verifications, Dispatch(dispatcher)

    def commands(self, work: Work) -> tuple[str, ...]:
        t = work.ticket
        owner = self.c.tickets.t.get(t.parent) if t.parent else None
        own = list((owner.work if owner else {}).get("verification", []))
        return tuple(own or self.c.oracle_commands())

    async def verify(self, work: Work, sha: str, commands: tuple[str, ...]) -> Verification:
        c, tid = self.c, work.ticket.id
        async with c.lock:
            key = self.svc.key(sha, commands)
            c.event("verify", f"verification of {sha[:12]} ({len(commands)} command(s))", tid)

        def sandbox(name: str) -> None:
            c.verifying[tid] = name
            c.host.touch()
        try:
            run = await self.svc.verify(key, f"ticket:{tid}", STEP, node=tid, on_sandbox=sandbox,
                                        dispatch=self.dispatch)
        finally:
            async with c.lock:
                c.verifying.pop(tid, None)
        full = c.json_doc(record_path(run.id)) if c.latest(record_path(run.id)) else None
        return Verification.model_validate(full) if full else (run.result or Verification(
            sha=sha, infra_error=f"verification {run.id}: {run.state}"))

    async def stop(self, ticket: str) -> None:
        return None


class Provision:
    """``controller:provision``: the approved environment, built."""

    def __init__(self, c: ChangeCore, provisioner: Provisioner, dispatcher: Dispatcher):
        self.c, self.provisioner, self.dispatch = c, provisioner, Dispatch(dispatcher)

    async def provision(self, work: Work) -> str | None:
        if self.c.cfg.environment is None:          # no environment stage: the default image
            async with self.c.lock:
                self.c.event("provision", "no environment stage: every sandbox uses the default "
                             "image", work.ticket.id)
                self.c.save()
            return None
        ticket = self.c.tickets.t[work.ticket.id]
        return await self.provisioner.provision(ticket, work,
                                                wanted=lambda: live(self.c, work) is not None,
                                                dispatch=self.dispatch)

    async def stop(self, ticket: str) -> None:
        return None


class Remote:
    """``controller:publish`` with a remote: the publisher pushes the result and opens (or
    brings up to date) its pull request, with the description the change's ``pr`` pointer is."""

    def __init__(self, c: ChangeCore, publisher: Publisher):
        self.c, self.publisher = c, publisher

    async def publish(self, work: Work, head: str) -> str:
        tier = self.c.change.tier
        labels = [LabelSpec("risk:", (f"risk:{tier}",) if tier else (), "d93f0b",
                            "Legion risk tier")]
        done = await self.publisher.publish(labels, head)
        async with self.c.lock:
            self.publisher.record(done)
            self.c.event("publish", f"pull request {done.url} ({done.branch} at {done.head[:12]})",
                         work.ticket.id)
            self.c.save()
        return done.url
