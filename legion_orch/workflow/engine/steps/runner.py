"""The step runner (plan: stages-and-steps.md §2): the one place that moves a node through its
step type's phases (:mod:`.step_types`) and owns its state – a phase that passes hands over to
the next, and after the last the node is accepted; at a step's boundaries it tells the layer
above (:class:`StepEvents`)."""
from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from ...model import (
    ESCALATED,
    LIVE_RUN,
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
from ..base.core import ChangeCore
from ..base.dispatch import StepDispatcher, Withdrawn
from .node_states import WAITING, NodeStates
from .phases import (
    Phase,
    PhaseContext,
    PhaseResult,
)

if TYPE_CHECKING:
    from ..services.provision import Provisioner
    from ..services.runs import RunLifecycle

from .step_types import StepType, step_for


# ---------------------------------------------------------------------------- the runner
class StepEvents(Protocol):
    """What the step runner tells the layer above it (the workflow runtime implements it): a
    step crossed one of its states. The runner calls nothing else above it."""

    def completed(self, node: Node) -> None:
        """The step is done (its last phase passed, or a round's step stands)."""

    def escalated(self, node: Node) -> None:
        """The step waits for a human."""

    def resumed(self, node: Node) -> None:
        """The step's escalation was resolved: it goes on."""

    def redo(self, step: str, feedback: Feedback) -> None:
        """An earlier step must do its work again (found wanting by this one)."""


class StepRunner:
    """The step layer: moves nodes through their step's phases and owns their states. Above it
    the workflow runtime starts, stops, resets and resumes steps; below it the phases do their
    work – as tasks (:meth:`run`), each awaiting its step's waits (:meth:`wait_until`) and a
    slot from the step's dispatcher – and hand back their results. Each running step holds a
    reservation with the dispatcher – a slot from its first work, given back while it waits on
    its conditions – until it is done, sent back or
    escalated (:meth:`dispatcher`). At the boundaries of a step's states – done, escalated,
    resumed, an earlier step redone – it tells the runtime, and nothing else does."""

    def __init__(self, core: ChangeCore, ctx: PhaseContext, lifecycle: RunLifecycle,
                 provisioner: Provisioner):
        self.c, self.ctx, self.lifecycle, self.provisioner = core, ctx, lifecycle, provisioner
        self.states = NodeStates()
        self.runtime: StepEvents = None  # type: ignore[assignment]   # set once it exists
        self._steps: dict[str, StepDispatcher] = {}   # node -> its step's dispatcher
        # (node, cycle) -> role -> a judge's escalation, held until the panel's others finish
        self._held: dict[tuple[str, int], dict[str, Escalate]] = {}
        self.waiting = 0                     # phase tasks waiting on the change's state

    @staticmethod
    def step(node: Node) -> StepType:
        return step_for(node)

    # ------------------------------------------------------------------ from the runtime (down)
    def start(self, node: Node, feedback: list[Feedback], rerun: bool = False) -> None:
        """The core started the node's step: a new cycle if it ran before, with the feedback
        it was sent back with. An agent that ran continues in its kept session (REWORK); a
        round's step that has nothing to redo stands (not one run again in place, ``rerun``: its
        work is wanted again); anything else enters its first phase."""
        node.approved = False                    # a gate is asked again
        if node.runs or node.status != PENDING:
            node.cycle += 1
            node.candidate_sha = node.accepted_sha = None
        for f in feedback:
            node.feedback.append(f.model_copy(update={"cycle": node.cycle}))
        step = self.step(node)
        self.release(node)
        if node.runs and step.role:
            self.states.reset(node)
            self.states.phase(node, REWORK)
            step.phases[0].start(self, node)
            return
        self.states.reset(node)
        if step.stands and not rerun and step.stands.holds(self.c, node):
            self.c.event(step.stands.kind, step.stands.why, node.id)
            step.stands.keep(self.c, node)
            self.accept(node)
            return
        self.enter(node)

    def enter(self, node: Node) -> None:
        """The node's first phase."""
        self.step(node).phases[0].enter(self, node, None)

    def reset(self, node: Node) -> None:
        """The core sent the step back or stopped it: its runs end, the node is pending."""
        for r in [self.c.runs[r] for r in node.runs if r in self.c.runs]:
            proc = self.c.procs.get(r.id)
            if proc and proc.returncode is None:
                proc.kill()
            if r.status == R_REPORTED:
                r.status = R_SUPERSEDED
        self.lifecycle.retire_runs(node)
        node.candidate_sha = node.accepted_sha = None
        node.approved = False
        self.release(node)
        self.states.reset(node)

    def resume(self, node: Node, to: NodeStatus, answered: bool = False) -> None:
        """The node's escalation is resolved – ``answered`` by a human, or retried: its step
        goes on in the phase ``to``, which takes up its work again (a verification, a merge, a
        gate's check)."""
        self.states.resume(node, to)
        self.runtime.resumed(node)
        phase = self.step(node).current(node)
        if phase is not None:
            phase.resume(self, node, answered)

    def resume_escalation(self, node: Node, answered: bool = False) -> None:
        """A human answered (the decision is on the node): the step goes on where its
        escalation said – one more cycle where a budget ran out, the phase it stopped in, or a
        new run in a new sandbox."""
        point = node.escalation.resume if node.escalation else Resume.READY
        if point == Resume.CRITIQUE_BUDGET:            # one more critique cycle: rework the plan
            node.extra_cycles += 1
            node.cycle += 1
            self.resume(node, REWORK, answered)
        elif point == Resume.REWORK_CYCLE:             # one more review cycle
            node.extra_cycles += 1
            cur = self.c.runs.get(node.current_run) if node.current_run else None
            if cur and cur.status == R_REPORTED:
                cur.status = R_SUPERSEDED
            node.cycle += 1
            node.candidate_sha = None
            self.resume(node, REWORK, answered)
        elif point in (Resume.VERIFYING, Resume.REVIEWING, Resume.MERGING, Resume.REWORK):
            self.resume(node, NodeStatus(point.value), answered)
        else:                                          # a new run, in a new sandbox
            self.lifecycle.retire_runs(node)
            self.resume(node, READY, answered)

    def retry(self, node: Node) -> None:
        """Retry: the failed run's sandbox goes first (a failed delete is reported and the
        retry goes ahead in a new sandbox), then the step goes on where its escalation said."""
        old = self._sandbox_to_remove(node)
        if old is None:
            self.resume_escalation(node)
            return
        node.escalation.retrying = True
        self.c.spawn(self._retry_after_removing(node.id, old))

    def _sandbox_to_remove(self, node: Node) -> str | None:
        """The escalated run's sandbox, if it still exists and no live run uses it."""
        esc = node.escalation
        run = self.c.runs.get(esc.run) if esc and esc.run else None
        if not run or not run.sandbox or run.sandbox_deleted:
            return None
        if any(r.sandbox == run.sandbox and r.status in LIVE_RUN for r in self.c.runs.values()):
            return None
        return run.sandbox

    async def _retry_after_removing(self, node_id: str, sandbox: str) -> None:
        ok = await self.lifecycle.sandboxes.delete(sandbox) if sandbox else True
        async with self.c.lock:
            node = self.c.nodes.get(node_id)
            esc = node.escalation if node else None
            if node is None or node.status not in WAITING or esc is None or not esc.retrying:
                return                  # abandoned or otherwise resolved meanwhile
            if not ok:
                self.c.event("sandbox", f"could not remove {sandbox}; retrying in a new sandbox",
                             node_id)
            if node.sandbox == sandbox:
                node.sandbox = None     # the next run gets a fresh sandbox
            self.resume_escalation(node)
            self.c.event("human_action", f"retry: resumed {esc.resume}", node_id)
            self.c.save()

    def resume_retries(self) -> None:
        """After a restart: finish retries that were removing their old sandbox."""
        for node in self.c.nodes.values():
            esc = node.escalation
            if node.status in WAITING and esc and esc.retrying:
                run = self.c.runs.get(esc.run) if esc.run else None
                self.c.spawn(self._retry_after_removing(node.id, run.sandbox if run else ""))

    def end(self, node: Node) -> None:
        """The change ended at this node (abandoned at its escalation)."""
        self.release(node)
        self.states.end(node)

    def again(self, node: Node) -> None:
        """A node outside the workflow's tree (the retrospective) runs again."""
        self.states.again(node)
        self.enter(node)

    def recover(self, node: Node, failed: Run | None = None) -> None:
        """After a restart: a run of the node that was live failed with the controller (its
        step escalates); otherwise its current phase takes its work up again."""
        phase = self.step(node).current(node)
        if node.status == ESCALATED:             # its sandboxes stay stopped meanwhile
            self.lifecycle.sandboxes.suspend_when_idle(self._sandboxes(node))
        if phase is None:
            return
        if failed is not None:
            self.apply(node, self.lifecycle.escalation(failed))
        elif self.active(node):
            phase.recover(self, node)

    def housekeeping(self, active: bool) -> None:
        """On a save: kept sandboxes whose hold ended are released, a released step's idle
        sandboxes stopped; once the change has ended, the steps that do not run after it give
        their reservations back."""
        self.lifecycle.release_sandboxes()
        self.lifecycle.sandboxes.suspend_due()
        if not active:
            for nid in [n for n in self._steps
                        if n not in self.c.nodes or not step_for(self.c.nodes[n]).runs_after_end]:
                self._steps.pop(nid).reservation.release()

    # ------------------------------------------------------------------ the phases' work
    def dispatcher(self, node: Node) -> StepDispatcher:
        """The step's dispatcher: the dispatcher with the step's reservation (taken now; it
        holds a slot from the first work on)."""
        work = self._steps.get(node.id)
        if work is None or work.released:
            work = self._steps[node.id] = StepDispatcher(self.ctx.dispatcher,
                                                         self.ctx.dispatcher.reserve())
        return work

    def held(self, node: Node, clear: bool = False) -> dict[str, Escalate]:
        """The escalations a phase holds for the node's cycle (the panel: until every judge has
        finished); ``clear``: given up."""
        key = (node.id, node.cycle)
        return self._held.pop(key, {}) if clear else self._held.setdefault(key, {})

    def release(self, node: Node) -> None:
        """The step is over (done, sent back, escalated, ended): its reservation goes back, its
        work still waiting is withdrawn, and its sandboxes are stopped once nothing live uses
        them (deleted ones aside: a judge's, a verifier's, an accepted run's)."""
        work = self._steps.pop(node.id, None)
        if work is not None:
            work.reservation.release()
        for key in [k for k in self._held if k[0] == node.id]:
            del self._held[key]
        self.lifecycle.sandboxes.suspend_when_idle(self._sandboxes(node))

    def _sandboxes(self, node: Node) -> set[str]:
        return {r.sandbox for r in (self.c.runs.get(rid) for rid in node.runs)
                if r is not None and r.sandbox and not r.sandbox_deleted}

    def run(self, node: Node, work: Callable[[StepDispatcher], Awaitable[None]]) -> None:
        """Start a phase's work as a task, with the step's dispatcher."""
        dispatch = self.dispatcher(node)

        async def task() -> None:
            try:
                await work(dispatch)
            except Withdrawn:                    # its step ended while it waited for a slot
                pass
        self.c.spawn(task())

    def active(self, node: Node) -> bool:
        """The node's step may work: the change is active (or it runs after the end)."""
        return self.c.change.status == "ACTIVE" or step_for(node).runs_after_end

    def stale(self, node: Node, work: StepDispatcher, phase: Phase) -> bool:
        """The work's step or phase is over: its result counts no more."""
        return work.released or self.c.nodes.get(node.id) is not node \
            or self.step(node).current(node) is not phase or not self.active(node)

    def waits_hold(self, node: Node) -> bool:
        """The step's waits (its ``dispatch_when``) for its first work."""
        return all(w.holds(self, node) for w in self.step(node).dispatch_when)

    async def wait_until(self, node: Node, work: StepDispatcher,
                         ready: Callable[[], bool]) -> bool:
        """Await ``ready`` – checked now and at each save of the change; while it does not
        hold, the step's reservation holds no slot. False: the step ended first (released, or
        the change ended)."""
        phase = self.step(node).current(node)
        while True:
            if phase is None or self.stale(node, work, phase):
                return False
            if ready():
                return True
            work.idle()                          # a waiting step holds no slot
            self.waiting += 1
            try:
                await self.c.next_save()
            finally:
                self.waiting -= 1

    # ------------------------------------------------------------------ from the phases (up)
    def phase(self, node: Node, to: NodeStatus) -> None:
        """A phase moves its node (entering it, starting its work, handing over)."""
        self.states.phase(node, to)

    def advance(self, node: Node, phase: Phase) -> None:
        """``phase`` passed: enter the next one, or accept the node after the last."""
        nxt = self.step(node).after(phase)
        if nxt is None:
            self.accept(node)
        else:
            nxt.enter(self, node, phase)

    def apply(self, node: Node, result: PhaseResult) -> None:
        """A phase's result that does not pass it: rework within the budget, an escalation, or
        an earlier step redone."""
        if isinstance(result, Rework):
            self.rework(node, result.feedback, result.source, result.budget_checked)
            if result.base and node.status == REWORK:
                node.base_sha = result.base
        elif isinstance(result, Escalate):
            if result.resume is None:            # a service's: where it resumes is the step's
                phase = self.step(node).current(node)
                result = dataclasses.replace(result, resume=(
                    phase.resume_after(self, node, result) if phase is not None
                    else self.step(node).rework_resume))
            node.feedback += result.feedback
            self.escalate(node, result.kind, result.question, result.resume, run=result.run,
                          missing_decision=result.missing_decision, log_tail=result.log_tail,
                          source=result.source, expected=result.expected)
        elif isinstance(result, Redo):
            self.runtime.redo(result.step, result.feedback)

    def rework(self, node: Node, feedback: list[Feedback], source: str,
               budget_checked: bool = False) -> None:
        """Evidence-driven rework within the cycle budget (G-RV5; ``budget_checked``: the
        producer's own budget was checked already)."""
        c = self.c
        node.feedback += feedback
        limit = c.cfg.limits.review_max_cycles + node.extra_cycles
        cur = c.runs.get(node.current_run) if node.current_run else None
        if cur and cur.status == R_REPORTED:
            cur.status = R_SUPERSEDED
        if not budget_checked and node.cycle >= limit:
            self.escalate(node, "cycle_budget",
                          f"G-RV5: {node.cycle} cycle(s) used without acceptance ({source})",
                          Resume.REWORK_CYCLE)
            return
        node.cycle += 1
        node.candidate_sha = None
        self.states.phase(node, REWORK)
        c.event("rework", f"{source}: rework cycle {node.cycle}", node.id)
        self.step(node).phases[0].start(self, node)         # its producer again, in its slot

    # ------------------------------------------------------------------ the boundaries
    def accept(self, node: Node) -> None:
        """The step's work is done: its runs are accepted, and the workflow is told."""
        self.release(node)
        self.states.accept(node)
        self.lifecycle.accept_runs(node)
        self.runtime.completed(node)

    def escalate(self, node: Node, kind: str, reason: str, resume: Resume, *,
                 run: str | None = None, missing_decision: str | None = None,
                 log_tail: str | None = None, source: FailureSource | None = None,
                 expected: bool = False) -> None:
        """A human decides; ``resume`` says where the step continues once they have.
        ``expected``: a decision that is due (a gate's approval), not a failure."""
        self.release(node)
        self.states.escalate(node, kind, reason, resume, run=run,
                             missing_decision=missing_decision, log_tail=log_tail, source=source,
                             expected=expected)
        if expected:
            self.c.event("human", f"awaiting a human decision: {reason}", node.id)
        else:
            self.c.event("escalation", f"{kind}: {reason}", node.id, run)
        self.runtime.escalated(node)
