"""A node's step as it runs (plan: stages-and-steps.md §2): its step type's phases, its states
and its reservation with the dispatcher – one object per node, made by the step runner, so
nothing below it is handed the node, the change or the runner again. Everything that lasts is
on the node; an active step holds only its reservation, and is made again after a restart."""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ...model import (
    AWAITING_HUMAN,
    ESCALATED,
    LIVE_RUN,
    MERGING,
    PENDING,
    R_REPORTED,
    R_SUPERSEDED,
    READY,
    REWORK,
    FailureSource,
    Feedback,
    Node,
    NodeStatus,
    Resume,
    Run,
)
from ...results import Escalate, Redo, Rework
from ..base.common import WorkflowError
from ..base.dispatch import StepDispatcher, Withdrawn
from .node_states import WAITING, IllegalNodeTransition
from .phases import ActivePhase, Approval, Merge, Phase, PhaseResult
from .step_types import StepType, step_for

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .approvals import Approved
    from .runner import StepRunner


def resume_status(point: Resume) -> NodeStatus:
    """The node's status a step resumes in: another cycle is a rework; READY is a new run."""
    if point in (Resume.CRITIQUE_BUDGET, Resume.REWORK_CYCLE):
        return REWORK
    return NodeStatus(point.value)


class ActiveStep:
    """One node's step: it moves the node through its step type's phases and owns the node's
    states. The step runner starts, resets, resumes and retries it; its phases – bound to it –
    do their work as tasks (:meth:`run`), each awaiting its waits (:meth:`wait_until`) and a
    slot from its dispatcher, and hand back their results. It holds a reservation with the
    dispatcher – a slot from its first work, given back while it waits on its conditions –
    until it is done, sent back or escalated (:attr:`dispatch`). At its boundaries – done,
    escalated, resumed, an earlier step redone – the runtime is told."""

    def __init__(self, runner: StepRunner, node: Node):
        self.runner, self.node = runner, node
        self.c, self.ctx = runner.c, runner.ctx
        self.lifecycle, self.provisioner = runner.lifecycle, runner.provisioner
        self.type: StepType = step_for(node)
        self._dispatch: StepDispatcher | None = None

    @property
    def states(self):
        return self.runner.states

    @property
    def events(self):
        return self.runner.runtime

    # ------------------------------------------------------------------ from the runner
    def start(self, feedback: list[Feedback], rerun: bool = False) -> None:
        """The core started the step: a new cycle if it ran before, with the feedback it was
        sent back with. An agent that ran continues in its kept session (REWORK); a round's
        step that has nothing to redo stands (not one run again in place, ``rerun``: its work
        is wanted again); anything else enters its first phase."""
        node, step = self.node, self.type
        if not node.runs and node.status == PENDING:  # its first start
            for hook in step.on_start:
                hook(self.c, node)
        node.approved = False                    # a gate is asked again
        node.agent_roles = step.agent_roles(self.c)   # their A2A cards, for the whole step
        self.ctx.agent_runner.open_step(node)          # its agents' cards (again for a new cycle)
        if node.runs or node.status != PENDING:
            node.cycle += 1
            node.candidate_sha = node.accepted_sha = None
        for f in feedback:
            node.feedback.append(f.model_copy(update={"cycle": node.cycle}))
        self.release()
        self.states.reset(node)
        if node.runs and step.role:
            self.states.phase(node, REWORK)
            self.at(step.phases[0]).start()
            return
        if step.stands and not rerun and step.stands.holds(self.c, node):
            self.c.event(step.stands.kind, step.stands.why, node.id)
            step.stands.keep(self.c, node)
            self.accept()
            return
        self.enter()

    def enter(self) -> None:
        """Its first phase."""
        self.at(self.type.phases[0]).enter()

    def reset(self) -> None:
        """The core sent the step back or stopped it: its runs end, the node is pending."""
        node = self.node
        for r in [self.c.runs[r] for r in node.runs if r in self.c.runs]:
            proc = self.c.procs.get(r.id)
            if proc and proc.returncode is None:
                proc.kill()
            if r.status == R_REPORTED:
                r.status = R_SUPERSEDED
        self.lifecycle.retire_runs(node)
        node.candidate_sha = node.accepted_sha = None
        node.approved = False
        self.release()
        self.states.reset(node)
        self.ctx.agent_runner.close_step(node.id)    # sent back or stopped: its step is over

    def resume(self, answered: bool = False) -> None:
        """Its escalation is resolved – ``answered`` by a human, or retried: it goes on where
        its escalation said – one more cycle where a budget ran out, the phase it stopped in,
        or a new run in a new sandbox."""
        node = self.node
        point = node.escalation.resume if node.escalation else Resume.READY
        node.reverify = True     # a human decided: the step's next verification is a fresh one
        if point == Resume.CRITIQUE_BUDGET:            # one more critique cycle: rework the plan
            node.extra_cycles += 1
            node.cycle += 1
        elif point == Resume.REWORK_CYCLE:             # one more review cycle
            node.extra_cycles += 1
            cur = self.c.runs.get(node.current_run) if node.current_run else None
            if cur and cur.status == R_REPORTED:
                cur.status = R_SUPERSEDED
            node.cycle += 1
            node.candidate_sha = None
        elif resume_status(point) == READY:            # a new run, in a new sandbox
            self.lifecycle.retire_runs(node)
        self._go_on(resume_status(point), answered)

    def override(self) -> None:
        """A human accepted it without its own checks: it finishes its work – its merge (only
        a step that merges can be overridden)."""
        if self.type.phase(Merge) is None:
            raise WorkflowError(f"{self.node.id}: its step cannot be overridden")
        self._go_on(MERGING, answered=True)

    async def approve(self, action: str, approver: str, payload: dict) -> Approved:
        """A human approved its gate (a step with an :class:`.Approval` phase, waiting for the
        decision): what approving records, as the gate's ``approves`` says. Answering its
        escalation, once it is approved, is the core's (the caller's)."""
        approval = self.type.phase(Approval)
        if approval is None or self.node.status != AWAITING_HUMAN:
            raise WorkflowError(f"{self.node.id} is not a gate waiting for a decision")
        if action not in approval.approves.actions:
            raise WorkflowError(f"action {action!r} not valid for {self.node.id} (one of "
                                f"{sorted(approval.approves.actions)})")
        return await approval.approves.approve(self.at(approval), approver, payload)

    def _go_on(self, to: NodeStatus, answered: bool) -> None:
        """Out of the escalation into the phase ``to``, which takes up its work again (a
        verification, a merge, a gate's check)."""
        self.states.resume(self.node, to)
        self.events.resumed(self.node)
        phase = self.current()
        if phase is not None:
            phase.resume(answered)

    def retry(self) -> None:
        """Retry: the failed run's sandbox goes first (a failed delete is reported and the
        retry goes ahead in a new sandbox), then it goes on where its escalation said."""
        old = self._sandbox_to_remove()
        if old is None:
            self.resume()
            return
        self.node.escalation.retrying = True
        self.c.spawn(self.retry_after_removing(old))

    def _sandbox_to_remove(self) -> str | None:
        """The escalated run's sandbox, if it still exists and no live run uses it."""
        esc = self.node.escalation
        run = self.c.runs.get(esc.run) if esc and esc.run else None
        if not run or not run.sandbox or run.sandbox_deleted:
            return None
        if any(r.sandbox == run.sandbox and r.status in LIVE_RUN for r in self.c.runs.values()):
            return None
        return run.sandbox

    async def retry_after_removing(self, sandbox: str) -> None:
        ok = await self.lifecycle.sandboxes.delete(sandbox) if sandbox else True
        node = self.node
        async with self.c.lock:
            esc = node.escalation
            if self.c.nodes.get(node.id) is not node or node.status not in WAITING \
                    or esc is None or not esc.retrying:
                return                  # abandoned or otherwise resolved meanwhile
            if not ok:
                self.c.event("sandbox", f"could not remove {sandbox}; retrying in a new sandbox",
                             node.id)
            if node.sandbox == sandbox:
                node.sandbox = None     # the next run gets a fresh sandbox
            self.resume()
            self.c.event("human_action", f"retry: resumed {esc.resume}", node.id)
            self.c.save()

    def end(self) -> None:
        """The change ended at this node (abandoned at its escalation)."""
        self.release()
        self.states.end(self.node)
        self.ctx.agent_runner.close_step(self.node.id)

    def again(self) -> None:
        """A node outside the workflow's tree (the retrospective) runs again."""
        self.states.again(self.node)
        self.enter()

    def recover(self, failed: Run | None = None) -> None:
        """After a restart: a run that was live failed with the controller (the step
        escalates); otherwise its current phase takes its work up again."""
        self.runner.adopted(self.node)
        phase = self.current()
        if self.node.status == ESCALATED:        # its sandboxes stay stopped meanwhile
            self.lifecycle.sandboxes.suspend_when_idle(self._sandboxes())
        if phase is None:
            return
        if failed is not None:
            self.apply(self.lifecycle.escalation(failed))
        elif self.may_work():
            phase.recover()

    # ------------------------------------------------------------------ its reservation
    @property
    def dispatch(self) -> StepDispatcher:
        """Its dispatcher: the dispatcher with its reservation (taken now; it holds a slot from
        the first work on)."""
        if self._dispatch is None or self._dispatch.released:
            d = self.ctx.dispatcher
            self._dispatch = StepDispatcher(d, d.reserve())
        return self._dispatch

    def release(self) -> None:
        """It is over (done, sent back, escalated, ended): its reservation goes back, its work
        still waiting is withdrawn, and its sandboxes are stopped once nothing live uses them
        (deleted ones aside: a judge's, a verifier's, an accepted run's)."""
        if self._dispatch is not None:
            self._dispatch.reservation.release()
            self._dispatch = None
        self.lifecycle.sandboxes.suspend_when_idle(self._sandboxes())

    def give_back(self) -> None:
        """Its reservation goes back (the change ended), its sandboxes untouched."""
        if self._dispatch is not None:
            self._dispatch.reservation.release()
            self._dispatch = None

    def _sandboxes(self) -> set[str]:
        return {r.sandbox for r in (self.c.runs.get(rid) for rid in self.node.runs)
                if r is not None and r.sandbox and not r.sandbox_deleted}

    # ------------------------------------------------------------------ its phases' work
    def at(self, phase: Phase) -> ActivePhase:
        """``phase`` (as its step type declares it) as it runs for this step: the only way a
        phase of a node is reached."""
        return ActivePhase(self, phase)

    def current(self) -> ActivePhase | None:
        """Its current phase, as it runs (None: pending, accepted or waiting)."""
        phase = self.type.phase_in(self.node.status)
        return self.at(phase) if phase is not None else None

    def run(self, phase: ActivePhase,
            work: Callable[[ActivePhase], Awaitable[None]] | None = None) -> None:
        """Start a phase's work as a task, with its dispatcher: its
        :meth:`~.ActivePhase.task`, or ``work``."""
        bound = ActivePhase(self, phase.phase, self.dispatch)

        async def task() -> None:
            try:
                await (work(bound) if work is not None else bound.task())
            except Withdrawn:                    # it ended while its work waited for a slot
                pass
            except Exception as e:               # the controller's own failure: a human decides
                await self._failed(bound, e)
        self.c.spawn(task())

    async def _failed(self, phase: ActivePhase, e: Exception) -> None:
        """A phase's work raised: unless it counts no more, the step escalates
        (``internal_error``, the controller's failure), so it is never left in its phase with
        nothing running. A retry resumes in that phase where it can (verifying, reviewing,
        merging again), else where the phase says."""
        log.exception("%s %s: %s failed", self.c.cid, self.node.id, type(phase.phase).__name__)
        async with self.c.lock:
            if self.stale(phase):
                return
            esc = Escalate("internal_error", f"{type(phase.phase).__name__} failed: "
                           f"{type(e).__name__}: {e}", None, source=FailureSource.CONTROLLER)
            esc = dataclasses.replace(esc, resume=self._resume_in(phase, esc))
            self.apply(esc)
            self.c.save()

    def _resume_in(self, phase: ActivePhase, esc: Escalate) -> Resume:
        status = self.node.status
        if status.value in Resume._value2member_map_ \
                and self.type.phase_in(status) is phase.phase:
            return Resume(status.value)
        return phase.resume_after(esc)

    def may_work(self) -> bool:
        """The change is active (or the step runs after the end)."""
        return self.c.change.status == "ACTIVE" or self.type.runs_after_end

    def stale(self, phase: ActivePhase) -> bool:
        """The work of ``phase`` counts no more: its reservation went back, the node was
        replaced, it moved on from the phase, or the change ended."""
        return (phase.dispatch is not None and phase.dispatch.released) \
            or self.c.nodes.get(self.node.id) is not self.node \
            or self.type.phase_in(self.node.status) is not phase.phase \
            or not self.may_work()

    def waits_hold(self) -> bool:
        """Its waits (its ``dispatch_when``) for its first work."""
        return all(w.holds(self) for w in self.type.dispatch_when)

    async def wait_until(self, phase: ActivePhase, ready: Callable[[], bool]) -> bool:
        """Await ``ready`` – checked now and at each save of the change; while it does not
        hold, its reservation holds no slot. False: ``phase``'s work counts no more first."""
        while True:
            if self.stale(phase):
                return False
            if ready():
                return True
            phase.dispatch.idle()                # a waiting step holds no slot
            self.runner.waiting += 1
            try:
                await self.c.next_save()
            finally:
                self.runner.waiting -= 1

    # ------------------------------------------------------------------ from its phases
    def move(self, to: NodeStatus) -> None:
        """A phase moves the node (entering it, starting its work, handing over)."""
        self.states.phase(self.node, to)

    def advance(self, phase: ActivePhase) -> None:
        """``phase`` passed: enter the next one, or accept the node after the last."""
        nxt = self.type.after(phase.phase)
        if nxt is None:
            self.accept()
        else:
            self.at(nxt).enter()

    def apply(self, result: PhaseResult) -> None:
        """A phase's result that does not pass it: rework within the budget, an escalation, or
        an earlier step redone."""
        node = self.node
        if isinstance(result, Rework):
            self.rework(result.feedback, result.source, result.budget_checked)
            if result.base and node.status == REWORK:
                node.base_sha = result.base
        elif isinstance(result, Escalate):
            if result.resume is None:            # a service's: where it resumes is the step's
                phase = self.current()
                result = dataclasses.replace(result, resume=(
                    phase.resume_after(result) if phase is not None
                    else self.type.rework_resume))
            node.feedback += result.feedback
            self.escalate(result.kind, result.question, result.resume, run=result.run,
                          missing_decision=result.missing_decision, log_tail=result.log_tail,
                          source=result.source, expected=result.expected)
        elif isinstance(result, Redo):
            self.events.redo(result.step, result.feedback)

    def rework(self, feedback: list[Feedback], source: str, budget_checked: bool = False) -> None:
        """Evidence-driven rework within the cycle budget (G-RV5; ``budget_checked``: the
        producer's own budget was checked already)."""
        c, node = self.c, self.node
        node.feedback += feedback
        limit = c.cfg.limits.review_max_cycles + node.extra_cycles
        cur = c.runs.get(node.current_run) if node.current_run else None
        if cur and cur.status == R_REPORTED:
            cur.status = R_SUPERSEDED
        if not budget_checked and node.cycle >= limit:
            self.escalate("cycle_budget",
                          f"G-RV5: {node.cycle} cycle(s) used without acceptance ({source})",
                          Resume.REWORK_CYCLE)
            return
        node.cycle += 1
        node.candidate_sha = None
        self.states.phase(node, REWORK)
        c.event("rework", f"{source}: rework cycle {node.cycle}", node.id)
        self.at(self.type.phases[0]).start()     # its producer again, in its slot

    # ------------------------------------------------------------------ its boundaries
    def accept(self) -> None:
        """Its work is done: its runs are accepted, and the workflow is told."""
        self.states.accept(self.node)        # before its reservation goes back: work waiting
        self.release()                       # for what it built is served on that release
        self.lifecycle.accept_runs(self.node)
        self.ctx.agent_runner.close_step(self.node.id)   # its step is over: its cards go
        self.events.completed(self.node)

    def escalate(self, kind: str, reason: str, resume: Resume, *, run: str | None = None,
                 missing_decision: str | None = None, log_tail: str | None = None,
                 source: FailureSource | None = None, expected: bool = False) -> None:
        """A human decides; ``resume`` says where it continues once they have. ``expected``: a
        decision that is due (a gate's approval), not a failure."""
        node, to = self.node, resume_status(resume)
        if self.type.phase_in(to) is None:
            raise IllegalNodeTransition(f"{node.id}: its step has no phase to resume at {resume}")
        self.release()
        self.states.escalate(node, kind, reason, resume, run=run,
                             missing_decision=missing_decision, log_tail=log_tail, source=source,
                             expected=expected)
        if expected:
            self.c.event("human", f"awaiting a human decision: {reason}", node.id)
        else:
            self.c.event("escalation", f"{kind}: {reason}", node.id, run)
        self.events.escalated(node)
