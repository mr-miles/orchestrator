"""Orchestrator phases (plan: stages-and-steps.md §2): the controller's work around an agent's, as
small objects that each end in one result – ok, rework (with feedback for the producer) or
escalate (a human decides).

A :class:`Phase` is one class per kind, holding its policies and its work, declared in a step
type and shared by every node of its kind. As it runs for a node it is an :class:`ActivePhase`
(the node's step, the phase, the step's dispatcher), which each of the phase's hooks is given.
A phase knows three things, and nothing about which kind of node it serves:

* **how it is entered** (``enter``): the node's status while the phase runs;
* **its work** (:meth:`ActivePhase.task`, the same shape for every phase): once it is
  ``ready`` (the same for every kind, from the ``waits_in`` it declares) it
  ``begin``s (its status), ``perform``s its work (through the step's dispatcher, a check) and
  then – under the lock – its ``result`` says what that means. Every hook is abstract: each
  kind says them all itself;
* **its result**: always ``result`` – from the work's record where it says it all
  (``Verify._interpret``, ``Panel._decide``), the role handler's for an agent, and for a merge
  the guarded merge itself (the guards and the merge hold at once).

What varies between nodes is passed in as small policy objects – which commands to verify
(:class:`CommandSource`), which judges sit on a panel (:class:`JudgeSource`), what a failed
verification means instead of rework – never decided by switching on the node's kind. A step is
an ordered list of phases (:mod:`.step_types`); :class:`.ActiveStep` moves a node from one phase
to the next.
"""
from __future__ import annotations

import asyncio
import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ... import governance
from ...model import (
    DEFINING,
    MERGING,
    ORACLE,
    R_ACCEPTED,
    R_REPORTED,
    READY,
    REVIEWING,
    REWORK,
    RUNNING,
    VERIFYING,
    CommandResult,
    Feedback,
    Node,
    NodeStatus,
    Oracle,
    PanelDecision,
    Readiness,
    Resume,
    Run,
    SetupPin,
    Verdict,
    Verification,
)
from ...results import Escalate, Ok, PhaseResult, Redo, Rework
from ...ship import evaluate
from ..base.common import WorkflowError, _slug
from ..base.core import ChangeCore
from ..base.ids import in_round
from ..sandboxes.plan import JUDGE, SandboxPlan
from ..services.verification import HEAD_CHECK, STEP, record_path

if TYPE_CHECKING:
    from ..base.dispatch import Dispatcher, StepDispatcher
    from ..sandboxes.sandboxes import Sandboxes
    from ..services.agent_runs import AgentRuns, Ended
    from ..services.agents import AgentRunner
    from ..services.provision import Provisioner
    from ..services.publish import Published, Publisher
    from ..services.runs import RunLifecycle
    from ..services.verification import Verifications
    from .active import ActiveStep
    from .approvals import Approves
    from .runner import StepEvents
    from .step_types import StepType

EXIT_NOT_FOUND = 127   # bash: command not found (126, not executable, is often a fixable repo file)


# the results every phase ends in, an agent's included (workflow/results.py)
__all__ = ["Escalate", "Ok", "PhaseResult", "Rework"]


@dataclass
class PhaseContext:
    """What phases work with: the change, its sandboxes, the dispatcher (shared: it runs work
    within the sandbox limit) and the change's agent runs."""
    c: ChangeCore
    sandboxes: Sandboxes
    dispatcher: Dispatcher = None  # type: ignore[assignment]
    agents: AgentRuns = None  # type: ignore[assignment]
    publisher: Publisher = None  # type: ignore[assignment]
    verifications: Verifications = None  # type: ignore[assignment]
    agent_runner: AgentRunner = None  # type: ignore[assignment]


class _Again:
    def __repr__(self) -> str:
        return "AGAIN"


#: a phase's work came too late: when its slot came the step no longer wanted it – it waits again
AGAIN = _Again()


@dataclass(frozen=True, eq=False)
class Phase(ABC):
    """A phase of a step, as a step type declares it: its kind's policies (which commands, which
    judges, what a failure means) and its work – shared by every node of its kind. Each kind
    implements every hook; a hook is given the phase as it runs for a node (``active``: an
    :class:`ActivePhase` – the node's step, the node, the step's dispatcher), so the phase
    itself holds nothing of any node. Whether its work may begin is the same for every kind
    (:meth:`ActivePhase.ready`), from the ``waits_in`` each kind declares."""
    #: the node statuses while this phase is the node's current one
    statuses: ClassVar[frozenset[NodeStatus]]
    #: the statuses in which its work is the step's first, so the step's waits must hold
    waits_in: ClassVar[frozenset[NodeStatus]]

    @abstractmethod
    def enter(self, active: ActivePhase) -> None:
        """The node moves into the phase (its first status), and its work starts."""

    @abstractmethod
    def start(self, active: ActivePhase) -> None:
        """Begin its work for the node's status (entered, resumed, or restarted)."""

    @abstractmethod
    def recover(self, active: ActivePhase) -> None:
        """After a restart: take its work up again."""

    @abstractmethod
    def resume(self, active: ActivePhase, answered: bool) -> None:
        """Its step's escalation was resolved – ``answered`` by a human, or retried – and it
        goes on in this phase."""

    @abstractmethod
    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        """Where the step resumes after an escalation in this phase that did not say (a failed
        run, a BLOCKED report)."""

    @abstractmethod
    def begin(self, active: ActivePhase) -> bool:
        """Under the lock, as its work begins: the node's status. True: it changed (saved)."""

    @abstractmethod
    async def perform(self, active: ActivePhase) -> object:
        """The work itself, outside the lock: through the step's dispatcher, or a check."""

    @abstractmethod
    async def result(self, active: ActivePhase, done: object) -> PhaseResult | None | _Again:
        """Under the lock: what the work (``done``) means – the phase's result (None: it waits
        for more; AGAIN: its work waits again)."""


class ActivePhase:
    """A phase as it runs for one node: made by the node's running step (:class:`.ActiveStep`)
    from itself, the declared phase and – while it works – the step's dispatcher. It is a
    phase's whole view of its node: the declarations it runs (``step``: the step type,
    ``phase``: the phase), the node, the change, and what its hooks may do – move the node
    within the step, pass the phase, apply a result, start its work. The work's shape is here,
    the same for every kind (:meth:`task`), with whether it may begin (:meth:`ready`),
    concluding a result, and whether its work still counts."""

    def __init__(self, running: ActiveStep, phase: Phase,
                 dispatch: StepDispatcher | None = None):
        self._running = running
        self.step: StepType = running.type
        self.phase, self.dispatch = phase, dispatch

    @property
    def node(self) -> Node:
        return self._running.node

    @property
    def c(self) -> ChangeCore:
        return self._running.c

    @property
    def ctx(self) -> PhaseContext:
        return self._running.ctx

    @property
    def events(self) -> StepEvents:
        """What the layer above is told (the workflow defines an accepted plan's tasks)."""
        return self._running.events

    @property
    def lifecycle(self) -> RunLifecycle:
        return self._running.lifecycle

    @property
    def provisioner(self) -> Provisioner:
        return self._running.provisioner

    # ------------------------------------------------------------ what its hooks may do
    def move(self, to: NodeStatus) -> None:
        """The node's status within the step."""
        self._running.move(to)

    def advance(self) -> None:
        """The phase passed: the node goes on to the step's next phase, or is accepted."""
        self._running.advance(self)

    def apply(self, result: PhaseResult) -> None:
        """A result that does not pass the phase: rework, an escalation, an earlier step redone."""
        self._running.apply(result)

    def run(self, work: Callable[[ActivePhase], Awaitable[None]] | None = None) -> None:
        """Start the phase's work as a task, with the step's dispatcher (its :meth:`task`, or
        ``work``)."""
        self._running.run(self, work)

    def waits_hold(self) -> bool:
        return self._running.waits_hold()

    # ------------------------------------------------------------ the phase's hooks, for it
    def enter(self) -> None:
        self.phase.enter(self)

    def start(self) -> None:
        self.phase.start(self)

    def recover(self) -> None:
        self.phase.recover(self)

    def resume(self, answered: bool) -> None:
        self.phase.resume(self, answered)

    def resume_after(self, escalation: Escalate) -> Resume:
        return self.phase.resume_after(self, escalation)

    def begin(self) -> bool:
        return self.phase.begin(self)

    async def perform(self) -> object:
        return await self.phase.perform(self)

    async def result(self, done: object) -> PhaseResult | None | _Again:
        return await self.phase.result(self, done)

    # ------------------------------------------------------------ its work, the same for all
    def ready(self) -> bool:
        """Its work may begin – the same for every kind, from what its phase declares: as the
        step's first work (``waits_in``) the step's waits hold. Checked now and at each save;
        while it does not hold, the step holds no slot. (Whether its sandbox's environment is
        built is its work's own: the dispatcher starts work only once it can.)"""
        return self.node.status not in self.phase.waits_in or self.waits_hold()


    async def task(self) -> None:
        """The phase's work: once it is :meth:`ready` (at once, or at a later save – the step's
        slot given back meanwhile) the phase ``begin``s under the lock, ``perform``s its work
        outside it, and then – under the lock, unless its step or phase ended meanwhile – its
        ``result`` says what the work means, which is concluded."""
        c = self.c
        while await self.wait_until(self.ready):
            async with c.lock:
                if self.stale():
                    return
                if self.begin():
                    c.save()
            done = await self.perform()
            async with c.lock:
                if self.stale():
                    return
                result = await self.result(done)
                if result is AGAIN:
                    c.save()
                    continue
                if result is None:          # nothing else will take its work up
                    raise WorkflowError(f"{type(self.phase).__name__} concluded nothing")
                self.conclude(result)       # type: ignore[arg-type]
                c.save()
            return

    def conclude(self, result: PhaseResult | None) -> None:
        """The phase's result goes to the step: ok passes the phase, anything else is applied
        (rework, escalate, an earlier step redone); None waits for more."""
        if isinstance(result, Ok):
            self.advance()
        elif result is not None:
            self.apply(result)

    def stale(self) -> bool:
        """Its step or this phase is over: its work's result counts no more."""
        return self._running.stale(self)

    async def wait_until(self, ready: Callable[[], bool]) -> bool:
        return await self._running.wait_until(self, ready)


# ---------------------------------------------------------------------------- policies
class CommandSource:
    """Which commands a verification runs, resolved when it runs."""

    def __call__(self, c: ChangeCore, node: Node) -> list[str]:
        raise NotImplementedError


class TaskCommands(CommandSource):
    """A build task's own ``verification`` commands."""

    def __call__(self, c: ChangeCore, node: Node) -> list[str]:
        return list(node.task.verification)


class AcceptanceCommands(CommandSource):
    """The oracle's acceptance commands."""

    def __call__(self, c: ChangeCore, node: Node) -> list[str]:
        return list((c.change.oracle or Oracle()).acceptance_commands)


class BaselineCommands(CommandSource):
    """The oracle's acceptance and integration commands (before any build)."""

    def __call__(self, c: ChangeCore, node: Node) -> list[str]:
        return c.baseline_commands()


class OracleCommands(CommandSource):
    """Everything that verifies the integration head: the oracle's commands and surfaces."""

    def __call__(self, c: ChangeCore, node: Node) -> list[str]:
        return c.oracle_commands()


class JudgeSource:
    """Which judges sit on a panel, resolved when it is dispatched."""

    def __call__(self, c: ChangeCore) -> tuple[str, ...]:
        raise NotImplementedError


class TierPanel(JudgeSource):
    """The review panel of the change's tier (set at H-1)."""

    def __call__(self, c: ChangeCore) -> tuple[str, ...]:
        return tuple(c.change.panel)


@dataclass(frozen=True)
class Judges(JudgeSource):
    """A fixed set of judges (the plan's critic)."""
    roles: tuple[str, ...]

    def __call__(self, c: ChangeCore) -> tuple[str, ...]:
        return self.roles


def _judges(source: JudgeSource | tuple[str, ...], c: ChangeCore | None) -> tuple[str, ...]:
    return source if isinstance(source, tuple) else source(c)


@dataclass(frozen=True)
class EscalateFailures:
    """Instead of rework: a failed verification goes to a human (nothing upstream can fix it in
    a cycle: integration)."""
    kind: str
    what: str                               # "{n} … failed": the question, with the count

    def __call__(self, res: Verification) -> Escalate:
        n = sum(1 for r in res.results if r.exit_code != 0)
        return Escalate(self.kind, self.what.format(n=n), Resume.READY)


class SetupFailurePolicy:
    """What a failed repository setup means for a verification that expects failures."""

    def __call__(self, c: ChangeCore, node: Node, setup: CommandResult, att: str,
                 escalation: Escalate) -> PhaseResult:
        raise NotImplementedError


class OracleReworks(SetupFailurePolicy):
    """G-V3 at preflight: the repository's setup failed, so nothing after it ran. Its round's
    oracle is done again – its author is sent the output, within the oracle's cycle budget
    (fixing it may need the plan's environment, which it then reports BLOCKED); after that a
    human decides."""

    def __call__(self, c: ChangeCore, node: Node, setup: CommandResult, att: str,
                 escalation: Escalate) -> PhaseResult:
        oracle = c.nodes.get(in_round(ORACLE, node.round)) or c.current(ORACLE)
        limit = c.cfg.limits.review_max_cycles + (oracle.extra_cycles if oracle else 0)
        if oracle is None or oracle.cycle >= limit:
            return escalation
        what = setup_failed_text(setup)
        c.event("rework", f"preflight: {what}: the oracle again", oracle.id)
        return Redo(oracle.id, Feedback(
            cycle=oracle.cycle, source="verification", attestations=[att],
            text=f"BLOCKER (preflight): {what}. Output: {setup.output[-800:]}. The acceptance "
                 "commands must run in a fresh clone after only the repository's setup "
                 "(mise.toml's setup task). Fix your tests or support files; if the fix is in "
                 "the setup (the plan's environment) or plan/oracle.json, report BLOCKED with "
                 "it as missing_decision."))


class Notes:
    """Observations recorded when a verification passes: (event kind, text)."""

    def __call__(self, c: ChangeCore, node: Node, res: Verification) -> list[tuple[str, str]]:
        raise NotImplementedError


class AlreadyPassing(Notes):
    """Before any build the commands are expected to fail: one that passes may not test the
    change (G-PF1)."""

    def __call__(self, c: ChangeCore, node: Node, res: Verification) -> list[tuple[str, str]]:
        return [("preflight", f"acceptance command `{r.command}` already passes before any "
                              "build: it may not test the change")
                for r in res.results if not r.setup and r.exit_code == 0]


class BaselineWarnings(Notes):
    """G-BL2: what already passes or differs from the agent's results, flagged at H-2."""

    def __call__(self, c: ChangeCore, node: Node, res: Verification) -> list[tuple[str, str]]:
        return [("baseline", w) for w in governance.baseline_warnings(c)]


# ---------------------------------------------------------------------------- agent work
async def agent_work(active: ActivePhase, role: str, plan: SandboxPlan, *,
                     wanted: Callable[[], bool],
                     judged_sha: str | None = None,
                     started: Callable[[Run], None] | None = None) -> Ended | _Again | None:
    """An agent of ``role`` working as ``plan`` says (where its working copy starts, whether it
    keeps its sandbox and session, pushes a branch), as a phase's work (a producer's, a
    judge's): whether the step may
    start it (G-B1, the environment), the work posted to the agent runner (which places it,
    prepares the working copy and runs the agent's turns in the step's dispatcher slot), and
    what it did – the role handler's result after an accepted report (read
    under the lock that ends the run), or the failure's or refusal's escalation. AGAIN: when
    its slot came the step no longer ``wanted`` it (its waits stopped holding); None: the step
    or the phase is over."""
    from ..services.agent_runs import Ended
    c, agents, node = active.c, active.ctx.agents, active.node
    async with c.lock:
        if active.stale():
            return None
        if refused := agents.preconditions(node, role):
            return Ended(None, refused)
    outcome, prepared = await active.ctx.agent_runner.publish_work(   # the runner launches it
        node, role, plan, judged_sha,
        wanted=lambda: not active.stale() and wanted(),
        started=started or (lambda run: None), dispatch=active.dispatch)
    async with c.lock:
        if outcome.skipped and prepared.refused is None:
            c.save()
            return AGAIN
        if active.stale():
            c.save()
            return None
        return Ended(prepared.run, prepared.refused or await agents.conclude(prepared.run))


@dataclass(frozen=True, eq=False)
class Produce(Phase):
    """The step's producer: an agent of ``role`` (:func:`agent_work`) dispatched once the
    step's waits hold (and G-H1 for work at the head). Its result – the role handler's, or a
    failure's or refusal's escalation – is the phase's."""
    role: str
    statuses: ClassVar = frozenset({READY, REWORK, RUNNING})
    waits_in: ClassVar = frozenset({READY, REWORK})        # each run of its agent

    def enter(self, active: ActivePhase) -> None:
        if active.node.status != REWORK:                # a rework continues in REWORK
            active.move(READY)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, REWORK):          # RUNNING: its agent works
            active.run()

    def recover(self, active: ActivePhase) -> None:
        """After a restart: a run whose agent ended its turn after an accepted report while the
        controller was down is concluded; a waiting step waits again."""
        run = active.c.runs.get(active.node.current_run) if active.node.current_run else None
        if active.node.status == RUNNING and run is not None and run.status == R_REPORTED \
                and not run.concluded:
            active.run(lambda a: self._conclude_later(a, run))
        else:
            self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        """A failed run or a BLOCKED report: where its step reworks."""
        return active.step.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        return False                                     # its agent's start moves it RUNNING

    async def perform(self, active: ActivePhase) -> object:
        c = active.c
        if active.step.judges_head:            # G-H1: the head is where the controller left it
            head = await c.git.integration_head(c.change.id)
            async with c.lock:
                if moved := c.head_moved(head):
                    return Escalate("head_moved", moved, Resume.READY)
        return await agent_work(
            active, self.role, active.step.sandbox, wanted=active.ready,
            started=lambda run: active.move(RUNNING))        # its agent works

    async def result(self, active: ActivePhase, done: object) -> PhaseResult | None | _Again:
        if done is None or done is AGAIN or isinstance(done, Escalate):
            return done                                          # type: ignore[return-value]
        return self._ended(active, done)                        # type: ignore[arg-type]

    async def _conclude_later(self, active: ActivePhase, run: Run) -> None:
        """After a restart: what an agent's accepted report means, to the phase."""
        from ..services.agent_runs import Ended
        async with active.c.lock:
            if active.stale() or run.concluded or run.status != R_REPORTED:
                return
            active.conclude(self._ended(active, Ended(run, await active.ctx.agents.conclude(run))))
            active.c.save()

    def _ended(self, active: ActivePhase, ended: Ended) -> PhaseResult | None:
        """The agent's turn ended: its result is the phase's."""
        if ended.run is not None:
            ended.run.concluded = True
        return ended.result


def combined(held: dict[str, Escalate]) -> Escalate:
    """The judges' escalations as one: the first's, asking about each."""
    first = next(iter(held.values()))
    if len(held) == 1:
        return first
    return dataclasses.replace(first, question="; ".join(
        f"{role}: {e.question}" for role, e in held.items()))




# ---------------------------------------------------------------------------- judges
async def judge_work(active: ActivePhase, role: str) -> Ended | None:
    """A judge's work at the candidate (a panel's, the critic's): its agent's work, once its
    environment is built. A judge that reported while the controller was down is concluded
    instead. Its run is done and, accepted, its sandbox retired as soon as it ends. None: the
    step or the phase ended first."""
    from ..services.agent_runs import Ended
    c, node = active.c, active.node
    async with c.lock:
        runs = [r for r in c.runs.values()
                if r.node_id == node.id and r.role == role and r.cycle == node.cycle]
        run = runs[-1] if runs else None
        if run is not None and run.status == R_REPORTED and not run.concluded:
            if active.stale():
                return None
            ended = Ended(run, await active.ctx.agents.conclude(run))
            judged(active, ended)
            c.save()
            return ended

    def wanted() -> bool:                                    # no review at the cycle yet
        return node.review(node.cycle, role) is None
    while await active.wait_until(wanted):
        done = await agent_work(active, role, JUDGE, wanted=wanted, judged_sha=node.candidate_sha)
        if done is AGAIN:
            continue
        if isinstance(done, Ended):
            async with c.lock:
                if not active.stale():
                    judged(active, done)
                    c.save()
        return done                                          # type: ignore[return-value]
    return None


def judged(active: ActivePhase, ended: Ended) -> None:
    """A judge's turn ended: its run is done and, accepted, its sandbox stopped – kept, with its
    session, for A2A questions about its judgement until its step ends (or the role's next
    cycle gets a fresh sandbox), then retired."""
    run = ended.run
    if run is not None and not run.concluded:
        run.concluded = True
        if run.status == R_REPORTED and not isinstance(ended.result, Escalate):
            run.status = R_ACCEPTED
            active.lifecycle.sandboxes.suspend_when_idle({run.sandbox})


def judge_resume(active: ActivePhase, escalation: Escalate) -> Resume:
    """Where a judging phase resumes: a judge's failed run – judged again (the other judges'
    reviews stand); a judge's BLOCKED report – where the step reworks."""
    return Resume.REVIEWING if escalation.kind == "run_failed" else active.step.rework_resume


@dataclass(frozen=True, eq=False)
class Panel(Phase):
    """The judges, one run per role at the candidate, in parallel. Unanimous PASS at the
    candidate is ok – recorded as the panel's pass, what the merge checks (G-A1); otherwise
    rework with the HIGH findings – or, when the same HIGH findings came back two cycles
    running, escalate (G-RV5)."""
    judges: JudgeSource | tuple[str, ...]
    statuses: ClassVar = frozenset({REVIEWING})
    waits_in: ClassVar = frozenset()

    def enter(self, active: ActivePhase) -> None:
        roles = self._roles(active)
        if not roles:                        # an empty panel: nothing to wait for
            self._record_pass(active, ())
            active.advance()
            return
        active.move(REVIEWING)
        active.c.event("review", f"panel {list(roles)}", active.node.id)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status == REVIEWING:
            active.run()

    def recover(self, active: ActivePhase) -> None:
        self.start(active)                                     # a judge that reported is concluded

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)                                     # the judges without a review

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return judge_resume(active, escalation)

    def begin(self, active: ActivePhase) -> bool:
        return False

    async def perform(self, active: ActivePhase) -> object:
        """Every judge still to review this cycle, in parallel under the step's reservation:
        role -> what its work did (None: the step or phase ended first)."""
        roles = self._missing(active)
        done = await asyncio.gather(*(judge_work(active, role) for role in roles))
        return dict(zip(roles, done, strict=True))

    async def result(self, active: ActivePhase, done: object) -> PhaseResult | None:
        """Every judge has finished: their escalations (a failure, a BLOCKED report, a refused
        dispatch) as one; otherwise the panel decides."""
        ended: dict[str, Ended | None] = done                    # type: ignore[assignment]
        if any(e is None for e in ended.values()):
            return None
        for e in ended.values():
            judged(active, e)                                    # once: concluded as it ended
        held = {role: e.result for role, e in ended.items()
                if e is not None and isinstance(e.result, Escalate)}
        if held:
            return combined(held)
        decision = self._decide(active)
        if isinstance(decision, Ok):
            self._record_pass(active, self._roles(active))
        return decision

    def _record_pass(self, active: ActivePhase, roles: tuple[str, ...]) -> None:
        """The evidence of its pass: which judges sat, at which candidate and cycle, citing
        their reviews – stored, and kept on the node (what the merge checks, G-A1)."""
        node, c = active.node, active.c
        reviews = [r.doc for r in (node.review(node.cycle, role) for role in roles) if r and r.doc]
        decision = PanelDecision(sha=node.candidate_sha, cycle=node.cycle, judges=list(roles),
                                 reviews=reviews)
        path = (f"tasks/{node.task.id}/panel.json" if node.task
                else f"control/panel/{_slug(node.id)}.json")
        decision.attestation = c.write_record(path, decision).attestation
        node.panel[str(node.cycle)] = decision

    def _roles(self, active: ActivePhase) -> tuple[str, ...]:
        """Its judges, for this change."""
        return _judges(self.judges, active.c)

    def _missing(self, active: ActivePhase) -> list[str]:
        """Its judges without a review at the node's cycle yet."""
        node = active.node
        return [r for r in self._roles(active) if node.review(node.cycle, r) is None]

    def _decide(self, active: ActivePhase) -> PhaseResult | None:
        """None until every judge has reported (records the cycle's finding fingerprints)."""
        node = active.node
        results = [node.review(node.cycle, r) for r in self._roles(active)]
        if any(r is None for r in results):
            return None
        if all(r.verdict == Verdict.PASS and r.sha == node.candidate_sha for r in results):
            return Ok()
        high = [f for r in results for f in r.findings if f.high]
        fps = sorted({f.fingerprint for f in high})
        node.finding_history.append(fps)
        fb = [Feedback(cycle=node.cycle, source="review",
                       text=f"{f.severity} ({f.dimension or '?'}): {f.title} – fix: {f.fix[:500]}",
                       attestations=f.attestations) for f in high]
        fb.append(Feedback(cycle=node.cycle, source="review", text="full review documents",
                           attestations=[r.doc for r in results if r.doc]))
        if len(node.finding_history) >= 2 and fps and node.finding_history[-1] == node.finding_history[-2]:
            return Escalate("stale_loop", "G-RV5: identical HIGH findings in two consecutive "
                            "review cycles", Resume.REWORK_CYCLE, feedback=fb)
        return Rework(fb, "review")


@dataclass(frozen=True, eq=False)
class Critique(Phase):
    """One judge of a document its step's agent wrote: the plan's critic (G-C2), the PR
    description's reviewer (G-PR3). Its PASS accepts the step; its REWORK sends the work back
    to the producer with the critique, within the critique budget – counted from the start of
    the step's fix round – else a human decides (``budget``: the guard that says so)."""
    role: str
    budget: str
    statuses: ClassVar = frozenset({REVIEWING})
    waits_in: ClassVar = frozenset()

    def enter(self, active: ActivePhase) -> None:
        active.move(REVIEWING)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status == REVIEWING:
            active.run()

    def recover(self, active: ActivePhase) -> None:
        self.start(active)                                     # a critic that reported is concluded

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return judge_resume(active, escalation)

    def begin(self, active: ActivePhase) -> bool:
        return False

    async def perform(self, active: ActivePhase) -> object:
        return await judge_work(active, self.role)

    async def result(self, active: ActivePhase, done: object) -> PhaseResult | None:
        """The critic's escalation (a failure, a BLOCKED report, a refused dispatch), or what
        its review says."""
        if done is None:
            return None
        ended: Ended = done                                      # type: ignore[assignment]
        judged(active, ended)
        if isinstance(ended.result, Escalate):
            return ended.result
        node = active.node
        review = node.review(node.cycle, self.role)
        if review is None:
            return None
        if review.verdict == Verdict.PASS:
            return Ok()
        feedback = [Feedback(
            cycle=node.cycle, source="critique", text="critique requested rework: "
            + "; ".join(f"{f.severity}: {f.title}" for f in review.findings),
            attestations=[review.doc] if review.doc else [])]
        limit = active.c.cfg.limits.critique_max_cycles + node.extra_cycles
        cycles = node.cycle - node.round_start_cycle + 1      # a fix round has its own budget
        if cycles >= limit:
            return Escalate("critique_budget", f"{self.budget}: {cycles} {self.role} cycles "
                            "without PASS",
                            Resume.CRITIQUE_BUDGET, feedback=feedback)
        return Rework(feedback, "critique", budget_checked=True)


@dataclass(frozen=True, eq=False)
class DefineTasks(Phase):
    """What accepting the plan means: its task graph's build tasks become the workflow's steps
    (a blank repository's scaffold first; a fix round: its new tasks only), defined by the
    workflow (:meth:`.StepEvents.define_tasks`) under the lock. Defined again after a restart
    in DEFINING: the workflow replaces a round's task nodes."""
    statuses: ClassVar = frozenset({DEFINING})
    waits_in: ClassVar = frozenset()

    def enter(self, active: ActivePhase) -> None:
        active.move(DEFINING)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status == DEFINING:
            active.run()

    def recover(self, active: ActivePhase) -> None:
        self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        return False

    async def perform(self, active: ActivePhase) -> object:
        return None                                      # its work is the workflow's, under the lock

    async def result(self, active: ActivePhase, done: object) -> PhaseResult:
        node = active.node
        ids = active.events.define_tasks(node)
        what = f"fix round {node.round}" if node.round > 1 else "plan"
        active.c.event("plan", f"{what} accepted by critic; {len(ids)} build task(s)"
                       + (f": {', '.join(ids)}" if node.round > 1 else ""), node.id)
        return Ok()


# ---------------------------------------------------------------------------- verification


@dataclass(frozen=True, eq=False)
class Verify(Phase):
    """G-V1: the commands at a commit in a fresh verifier sandbox, after the repository's setup
    (§5.3c), which stops the run if it fails (G-V3).

    ``expect_failures``: before any build the commands are expected to fail, so failures are
    recorded, not reworked. ``at_head``: verify the integration head (no agent produced a
    candidate). ``on_rework``: a failure escalates instead of reworking. ``on_setup_failed``:
    who fixes a failed setup when failures are expected. ``notes``: what to record on a pass."""
    commands: CommandSource | tuple[str, ...]
    expect_failures: bool = False
    at_head: bool = False
    on_rework: EscalateFailures | None = None
    on_setup_failed: SetupFailurePolicy | None = None
    notes: Notes | None = None
    waits_in: ClassVar = frozenset({READY})                 # at the head: the step's first work

    @property
    def statuses(self) -> frozenset[NodeStatus]:  # type: ignore[override]
        # at the head (no producer): READY until dispatched, then VERIFYING
        return frozenset({READY, VERIFYING}) if self.at_head else frozenset({VERIFYING})

    def _interpret(self, res: Verification, cycle: int, att: str | None) -> PhaseResult:
        """What the recorded verification (``att``) means. A setup failure is escalated as
        ``setup_failed`` when failures are expected (nothing was built yet that could have
        caused it); otherwise it fails like any command."""
        fails = [r for r in res.results if r.exit_code != 0]
        unrunnable = [r for r in fails if r.exit_code == EXIT_NOT_FOUND and not r.setup]
        setup = setup_failure(res)
        if res.infra_error:
            return Escalate("verify_infra", res.infra_error, Resume.VERIFYING)
        if unrunnable:
            # G-V2: the verifier image lacks a command – the agent cannot fix that, so no
            # rework cycle is spent; a human fixes the image and retries (re-verifies).
            return Escalate("verify_unrunnable",
                            "G-V2: command(s) not found in the verifier sandbox: "
                            + ", ".join(f"`{r.command}`" for r in unrunnable), Resume.VERIFYING,
                            log_tail="\n".join(r.output[-800:] for r in unrunnable))
        if setup and self.expect_failures:
            return Escalate("setup_failed", f"G-V3: {setup_failed_text(setup)}", Resume.VERIFYING,
                            log_tail=setup.output[-800:])
        if self.expect_failures or res.ok:
            return Ok(res)
        result = Rework([Feedback(cycle=cycle, source="verification",
                                  text=f"BLOCKER: `{r.command}` exited {r.exit_code}: "
                                       f"{r.output[-800:]}", attestations=[att] if att else [])
                         for r in fails], "verification")
        return self.on_rework(res) if self.on_rework else result

    def enter(self, active: ActivePhase) -> None:
        """After a producer, its candidate is verified at once (in the step's slot); at the head
        (nothing produced it) the step starts READY, like any step's first work."""
        active.move(READY if self.at_head else VERIFYING)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, VERIFYING):
            active.run()

    def recover(self, active: ActivePhase) -> None:
        self.start(active)                                     # verified again

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)                                     # verified again (ActiveStep.resume)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        if active.node.status != READY:
            return False
        active.node.candidate_sha = active.c.change.integration_head      # at the head: what it verifies
        active.move(VERIFYING)
        return True

    async def perform(self, active: ActivePhase) -> Verification:
        """The verification service's result for the candidate and these commands: a commit
        verified already (as it was pushed, say) is not verified again. Its live log shows on
        the node while it runs. A new verification follows on in the step's slot (work in flight
        goes first), within the verification cap."""
        node, c = active.node, active.c
        commands = self._to_run(active)
        svc = active.ctx.verifications
        async with c.lock:
            key = svc.key(node.candidate_sha, commands)
            force, node.reverify = node.reverify, False
            known = None if force else svc.result(key)
            c.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                    f"({'setup and ' if c.change.setup_pin else ''}{len(commands)} command(s))"
                    + (f": verified already ({known.id})" if known else ""), node.id)

        def live(sandbox: str) -> None:
            c.verifying[node.id] = sandbox                # its log, live (the UI shows it)
            c.host.touch()
        try:
            run = await svc.verify(key, f"step:{node.id}",
                                   HEAD_CHECK if self.at_head else STEP, node=node.id,
                                   on_sandbox=live, force=force, dispatch=active.dispatch)
        finally:
            async with c.lock:
                c.verifying.pop(node.id, None)
        full = c.json_doc(record_path(run.id)) if c.latest(record_path(run.id)) else None
        res = Verification.model_validate(full) if full else (run.result or Verification(
            sha=node.candidate_sha, infra_error=f"verification {run.id}: {run.state}"))
        return res.model_copy(update={"run": run.id, "cached": known is not None})

    async def result(self, active: ActivePhase, done: object) -> PhaseResult | None:
        """The verification is recorded, then interpreted; on a pass its notes are recorded, and
        a failed setup is what its policy makes of it."""
        node = active.node
        if node.status != VERIFYING:
            return None
        c, res = active.c, done
        assert isinstance(res, Verification)
        path, key = self._record(active)
        att = c.write_record(path, res).attestation
        # The node keeps a compact copy (the stored record has the command output).
        node.verification[key] = res.model_copy(update={
            "attestation": att,
            "results": [r.model_copy(update={"output": ""}) for r in res.results]})
        result = self._interpret(res, node.cycle, att)
        if isinstance(result, Ok):
            for kind, text in self.notes(c, node, res) if self.notes else []:
                c.event(kind, text, node.id)
        elif isinstance(result, Escalate) and result.kind == "setup_failed" \
                and self.on_setup_failed:
            result = self.on_setup_failed(c, node, setup_failure(res), att, result)
        return result

    def _to_run(self, active: ActivePhase) -> tuple[str, ...]:
        """The commands it runs, for this change and node."""
        commands = self.commands
        return commands if isinstance(commands, tuple) else tuple(commands(active.c, active.node))

    def _record(self, active: ActivePhase) -> tuple[str, str]:
        """Where the verification is stored, and its key on the node: a task's per cycle."""
        node = active.node
        if node.task:
            return f"tasks/{node.task.id}/verification.json", str(node.cycle)
        return f"control/verification/{_slug(node.id)}.json", str(node.kind)


def setup_failure(res: Verification) -> CommandResult | None:
    return next((r for r in res.results if r.setup and r.exit_code != 0), None)


def setup_failed_text(r: CommandResult) -> str:
    return (f"the repository's setup `{r.command}` exited {r.exit_code} in a fresh verifier "
            "sandbox, so no acceptance command ran")


# ---------------------------------------------------------------------------- merge


@dataclass(frozen=True, eq=False)
class Merge(Phase):
    """G-A1 – the evidence on the node: a passing verification and the panel's recorded pass at
    the candidate (unless overridden: the panel is waived, verification is not) – and G-H1 (the
    integration head is where the controller left it), then the merge into the integration
    branch; a conflict is rework onto the new head (G-A2). Under the lock. Ok: the new head."""
    statuses: ClassVar = frozenset({MERGING})
    waits_in: ClassVar = frozenset()

    def enter(self, active: ActivePhase) -> None:
        active.move(MERGING)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status == MERGING:                  # entered, resumed, overridden, restarted
            active.run()

    def recover(self, active: ActivePhase) -> None:
        self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        return False

    async def perform(self, active: ActivePhase) -> object:
        return None                                      # its work is all under the lock

    async def result(self, active: ActivePhase, done: object) -> PhaseResult:
        """It has no work outside the lock: its guards and the merge hold at once."""
        node = active.node
        from ..services import cleanup
        c = active.c
        sha = node.candidate_sha
        result = await self._merge(active)
        if isinstance(result, Ok):
            node.accepted_sha = sha
            run = c.runs.get(node.current_run) if node.current_run else None
            await cleanup.delete_run_branch(c, run.branch if run else None, node.id)
            c.event("accepted", f"merged {sha[:12]} -> integration {result.value[:12]}",
                    node.id)
        return result

    async def _merge(self, active: ActivePhase) -> PhaseResult:
        """G-A1 for its step's judges, G-H1, then the merge (or the conflict's rework)."""
        c, node = active.c, active.node
        sha = node.candidate_sha
        ver = node.current_verification()
        v = []
        if not ver or not ver.ok or ver.sha != sha:
            v.append("G-A1: no passing deterministic verification at candidate")
        panel = node.current_panel()
        if not node.override and (not panel or panel.sha != sha):
            v.append("G-A1: no panel PASS at candidate")
        if v:
            return Escalate("accept_guard", "; ".join(v), Resume.REWORK)
        if moved := c.head_moved(await c.git.integration_head(c.change.id)):
            return Escalate("head_moved", moved, Resume.MERGING)
        ok, out = await c.git.merge(c.cid, sha, f"legion {c.change.id}: {node.title} "
                                                f"(cycle {node.cycle})")
        if not ok:
            head = await c.git.integration_head(c.change.id)
            return Rework([Feedback(
                cycle=node.cycle, source="merge",
                text=f"G-A2: merge conflict with integration; rebase onto {head} "
                     f"and force-push your branch. Details: {out[-600:]}",
                attestations=[f"git:{c.workspace}@{head}"])], "merge", base=head)
        c.change.integration_head = out
        return Ok(out)


# ---------------------------------------------------------------------------- workspace
@dataclass(frozen=True)
class Synced:
    """What syncing the workspace found: the base and what it holds."""
    base: str
    blank: bool
    pin: SetupPin | None


@dataclass(frozen=True, eq=False)
class SyncWorkspace(Phase):
    """The change's first step: its workspace's hub brought up to date with the source (cloned
    if there is none yet; one sync of a hub at a time), and the change based on the base
    branch's commit – its integration branch, whether the repository is blank (§5.3b) and the
    base's setup (§5.3c). A hub that cannot follow its source escalates (what differs): the
    decision owner fixes the hub or the source and retries. Once the change has a base, it
    keeps it: the step run again (its stage sent back) passes at once."""
    statuses: ClassVar = frozenset({READY, RUNNING})
    waits_in: ClassVar = frozenset()

    def enter(self, active: ActivePhase) -> None:
        active.move(READY)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, RUNNING):         # RUNNING: syncing (after a restart)
            active.run()

    def recover(self, active: ActivePhase) -> None:
        self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)                                   # synced again

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return Resume.READY

    def begin(self, active: ActivePhase) -> bool:
        active.move(RUNNING)
        return True

    async def perform(self, active: ActivePhase) -> object:
        """The sync and what the base holds – or what keeps the hub from following its
        source."""
        from ....git import WorkspaceUnsynced, is_blank_tree
        from ... import setup
        c = active.c
        if c.change.base_sha:
            return None
        try:
            base = await c.git.sync()
        except WorkspaceUnsynced as e:
            return e
        blank = is_blank_tree(await c.git.tree_paths(base))
        # not in a blank repository, where what a setup needs does not exist yet
        return Synced(base, blank, None if blank else await setup.pin_at_base(c.git, base))

    async def result(self, active: ActivePhase, done: object) -> PhaseResult:
        from ....git import WorkspaceUnsynced
        c, ch = active.c, active.c.change
        if done is None:                                     # it has its base already
            return Ok()
        if isinstance(done, WorkspaceUnsynced):
            return Escalate("workspace_unsynced", f"workspace {ch.workspace}: {done}",
                            Resume.READY)
        synced: Synced = done                                # type: ignore[assignment]
        await c.git.init_integration(ch.id, synced.base)
        ch.base_sha = ch.integration_head = synced.base
        ch.blank_repo, ch.setup_pin = synced.blank, synced.pin
        c.write_record("control/workspace.json", {
            "workspace": ch.workspace, "source": c.git.source, "base_branch": c.git.base_branch,
            "base": synced.base, "blank_repo": synced.blank})
        c.event("workspace", f"{ch.workspace} synced: the change is based on "
                f"{synced.base[:12]}" + (" (a blank repository: the plan starts with a "
                                          "scaffold task)" if synced.blank else ""),
                active.node.id)
        return Ok()


# ---------------------------------------------------------------------------- publish
class PrLabels(ABC):
    """One family of a pull request's labels: which of them the change gets."""
    prefix: ClassVar[str]
    color: ClassVar[str]
    description: ClassVar[str]

    @abstractmethod
    def names(self, c: ChangeCore) -> tuple[str, ...]:
        """The family's labels the change's pull request has."""


@dataclass(frozen=True)
class RiskLabel(PrLabels):
    """``risk:<tier>``, from the tier approved at H-1."""
    prefix: ClassVar = "risk:"
    color: ClassVar = "d93f0b"
    description: ClassVar = "The change's risk tier (Legion)"

    def names(self, c: ChangeCore) -> tuple[str, ...]:
        return (f"risk:{c.change.tier}",) if c.change.tier else ()


@dataclass(frozen=True, eq=False)
class OpenPullRequest(Phase):
    """After H-3: the change published – its integration branch pushed to the workspace's
    source on GitHub as ``<publish_prefix><change-id>`` and a pull request opened from it, ready
    for review, with the reviewed description, the change's id and its labels (``labels``: one
    policy per family). Run again (a retry, a restart), it brings the pull request up to date.
    A failure (authentication, a branch that moved, GitHub) escalates; a retry publishes
    again."""
    labels: tuple[PrLabels, ...]
    statuses: ClassVar = frozenset({READY, RUNNING})
    waits_in: ClassVar = frozenset()

    def enter(self, active: ActivePhase) -> None:
        active.move(READY)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, RUNNING):         # RUNNING: publishing (a restart)
            active.run()

    def recover(self, active: ActivePhase) -> None:
        self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)                                   # published again

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return Resume.READY

    def begin(self, active: ActivePhase) -> bool:
        active.move(RUNNING)
        return True

    async def perform(self, active: ActivePhase) -> object:
        """The pull request as published – or what kept it from being."""
        from ....git import GitHubError
        from ..services.publish import LabelSpec
        specs = [LabelSpec(p.prefix, p.names(active.c), p.color, p.description)
                 for p in self.labels]
        try:
            return await active.ctx.publisher.publish(specs)
        except GitHubError as e:
            return e

    async def result(self, active: ActivePhase, done: object) -> PhaseResult:
        from ....git import GitHubError
        if isinstance(done, GitHubError):
            return Escalate("publish_failed", str(done), Resume.READY)
        published: Published = done                          # type: ignore[assignment]
        active.ctx.publisher.record(published)
        active.c.event("publish", f"pull request {published.url} ({published.branch} at "
                       f"{published.head[:12]})", active.node.id)
        return Ok()


# ---------------------------------------------------------------------------- environment
@dataclass(frozen=True, eq=False)
class BuildImages(Phase):
    """Provisioning, as a step's phase: once its waits hold, the provisioner builds the approved
    environment's images and checks the setup (G-PV1, G-PV3); its result is the phase's."""
    statuses: ClassVar = frozenset({READY, RUNNING})
    waits_in: ClassVar = frozenset({READY})                 # the environment is quiet

    def enter(self, active: ActivePhase) -> None:
        active.move(READY)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, RUNNING):
            active.run()

    def recover(self, active: ActivePhase) -> None:
        self.start(active)                                     # built again

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)                                     # built again

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        if active.node.status != READY:
            return False
        active.move(RUNNING)
        active.c.event("provision", "provisioning the approved environment", active.node.id)
        return True

    async def perform(self, active: ActivePhase) -> object:
        """The provisioner's work: the images built and the setup checked (G-PV1, G-PV3)."""
        return await active.provisioner.provision(active.node, dispatch=active.dispatch)

    async def result(self, active: ActivePhase, done: object) -> PhaseResult | None:
        """What the provisioner found is the phase's result."""
        return done if active.node.status == RUNNING else None          # type: ignore[return-value]


# ---------------------------------------------------------------------------- gates
class GateChecks:
    """What a gate checks before it passes (its preconditions): evaluated without the lock,
    then read under it – the unmet ones by name."""

    async def evaluate(self, c: ChangeCore) -> object:
        return None

    def unmet(self, c: ChangeCore, node: Node, evaluated: object) -> tuple[str, ...]:
        raise NotImplementedError


class ShipGates(GateChecks):
    """H-3's: the tier's ship gates (§6), recorded as the change's readiness. Those only its own
    approval satisfies are not checked before it is approved."""
    BY_APPROVAL = frozenset({"explicit_human_approval", "whole_change_acceptance_evidence"})

    async def evaluate(self, c: ChangeCore) -> Readiness:
        return await evaluate(c)

    def unmet(self, c: ChangeCore, node: Node, evaluated: object) -> tuple[str, ...]:
        from ...model import H_CHANGE
        result: Readiness = evaluated                # type: ignore[assignment]
        result.attestation = c.write_record("control/ship/readiness.json", result).attestation
        c.change.ship = result
        approved = H_CHANGE in c.change.approvals
        unmet = tuple(g for g, r in result.gates.items() if r.status != "satisfied"
                      and (approved or g not in self.BY_APPROVAL))
        c.event("ship", f"readiness: {result.verdict}"
                + (f" ({len(unmet)} gate(s) unmet)" if unmet else ""), node.id)
        return unmet


@dataclass(frozen=True)
class AutomaticBelow:
    """Nobody approves the gate below this tier rank: it passes once its checks hold."""
    rank: int

    def __call__(self, c: ChangeCore) -> bool:
        from ... import risk
        return not c.change.tier or risk.rank(c.change.tier) < self.rank




@dataclass(frozen=True, eq=False)
class Approval(Phase):
    """A gate: the stage's end, a human's decision. Its work checks what the gate needs
    (``checks``, e.g. the ship gates) and then – unless it passes by itself (``automatic``) –
    waits for a human: an *expected* escalation (``AWAITING_HUMAN``), which approving answers.
    Answered, it checks again: nothing still to approve (``approves``) and its checks met, it
    passes; unmet checks escalate again (a re-check is a retry). A new attempt (the stage sent
    back) asks again. What approving records, and what is still to approve, is ``approves``."""
    approves: Approves
    checks: GateChecks | None = None
    automatic: AutomaticBelow | None = None
    statuses: ClassVar = frozenset({READY, RUNNING})
    waits_in: ClassVar = frozenset()

    def enter(self, active: ActivePhase) -> None:
        active.move(READY)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, RUNNING):         # RUNNING: checking (after a restart)
            active.run()

    def recover(self, active: ActivePhase) -> None:
        self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        if answered:
            active.node.approved = True                 # it stays approved in this attempt
        self.start(active)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        active.move(RUNNING)               # checking
        return True

    async def perform(self, active: ActivePhase) -> object:
        return await self.checks.evaluate(active.c) if self.checks else None

    async def result(self, active: ActivePhase, done: object) -> PhaseResult:
        node = active.node
        c = active.c
        unmet = self.checks.unmet(c, node, done) if self.checks else ()
        approved = node.approved or bool(self.automatic and self.automatic(c))
        pending = self.approves.pending(c) if approved else []
        if not approved:
            ask = f"{node.title}" + (f" (not met yet: {', '.join(unmet)})" if unmet else "")
            return Escalate("awaiting_approval", ask, Resume.READY, expected=True)
        if pending:
            return Escalate("awaiting_approval", "still to approve: " + ", ".join(pending),
                            Resume.READY, expected=True)
        if unmet:
            return Escalate("preconditions_unmet", ", ".join(unmet), Resume.READY, expected=True)
        return Ok()

