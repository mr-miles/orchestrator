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
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ....sandbox import VERIFIER_PREFIX
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
    Verdict,
    Verification,
)
from ...results import Escalate, Ok, PhaseResult, Redo, Rework
from ...ship import evaluate
from ..base.common import _slug
from ..base.core import ChangeCore
from ..base.ids import in_round
from ..base.work import COMMAND, PREPARE, SETUP, Outcome
from ..sandboxes.plan import JUDGE, SandboxPlan

if TYPE_CHECKING:
    from ..base.dispatch import Dispatcher, StepDispatcher
    from ..sandboxes.sandboxes import Sandboxes
    from ..services.agent_runs import AgentRuns, Ended
    from .active import ActiveStep

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
    """A phase as it runs for one node: made from the node's step (:class:`.ActiveStep`), the
    declared phase and – while it works – the step's dispatcher. It is what the active step
    works with: each of the phase's hooks is called with it, and the work's shape is here, the
    same for every kind (:meth:`task`), with whether it may begin (:meth:`ready`), concluding
    a result, and whether its work still counts."""

    def __init__(self, step: ActiveStep, phase: Phase, dispatch: StepDispatcher | None = None):
        self.step, self.phase, self.dispatch = step, phase, dispatch

    @property
    def node(self) -> Node:
        return self.step.node

    @property
    def c(self) -> ChangeCore:
        return self.step.c

    @property
    def ctx(self) -> PhaseContext:
        return self.step.ctx

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
        return self.node.status not in self.phase.waits_in or self.step.waits_hold()


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
                self.conclude(result)       # type: ignore[arg-type]
                c.save()
            return

    def conclude(self, result: PhaseResult | None) -> None:
        """The phase's result goes to the step: ok passes the phase, anything else is applied
        (rework, escalate, an earlier step redone); None waits for more."""
        if isinstance(result, Ok):
            self.step.advance(self)
        elif result is not None:
            self.step.apply(result)

    def stale(self) -> bool:
        """Its step or this phase is over: its work's result counts no more."""
        return self.step.stale(self)

    async def wait_until(self, ready: Callable[[], bool]) -> bool:
        return await self.step.wait_until(self, ready)


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
    start it (G-B1, the environment), its :class:`.Work` (made by the change's agent runs: the
    run record, the working copy's preparation, the agent's turn) dispatched on the step's
    dispatcher, and what it did – the role handler's result after an accepted report (read
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
    work, prepared = agents.work(
        node, role, plan, judged_sha,
        wanted=lambda: not active.stale() and wanted(),
        started=started or (lambda run: None))
    outcome = await active.dispatch.run(work)
    async with c.lock:
        if prepared.run is not None:
            agents.finish(prepared.run, outcome)       # however the work ended
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
            active.step.move(READY)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, REWORK):          # RUNNING: its agent works
            active.step.run(active)

    def recover(self, active: ActivePhase) -> None:
        """After a restart: a run whose agent ended its turn after an accepted report while the
        controller was down is concluded; a waiting step waits again."""
        run = active.c.runs.get(active.node.current_run) if active.node.current_run else None
        if active.node.status == RUNNING and run is not None and run.status == R_REPORTED \
                and not run.concluded:
            active.step.run(active, lambda a: self._conclude_later(a, run))
        else:
            self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        """A failed run or a BLOCKED report: where its step reworks."""
        return active.step.type.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        return False                                     # its agent's start moves it RUNNING

    async def perform(self, active: ActivePhase) -> object:
        c = active.c
        if active.step.type.judges_head:            # G-H1: the head is where the controller left it
            head = await c.git.integration_head(c.change.id)
            async with c.lock:
                if moved := c.head_moved(head):
                    return Escalate("head_moved", moved, Resume.READY)
        return await agent_work(
            active, self.role, active.step.type.sandbox, wanted=active.ready,
            started=lambda run: active.step.move(RUNNING))        # its agent works

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
    """A judge's turn ended: its run is done and, accepted, its sandbox retired (once)."""
    run = ended.run
    if run is not None and not run.concluded:
        run.concluded = True
        if run.status == R_REPORTED and not isinstance(ended.result, Escalate):
            run.status = R_ACCEPTED
            active.step.lifecycle.sandboxes.delete_later(run.sandbox)


def judge_resume(active: ActivePhase, escalation: Escalate) -> Resume:
    """Where a judging phase resumes: a judge's failed run – judged again (the other judges'
    reviews stand); a judge's BLOCKED report – where the step reworks."""
    return Resume.REVIEWING if escalation.kind == "run_failed" else active.step.type.rework_resume


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
            active.step.advance(active)
            return
        active.step.move(REVIEWING)
        active.c.event("review", f"panel {list(roles)}", active.node.id)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status == REVIEWING:
            active.step.run(active)

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
    """The plan's critic (G-C2): one judge of the drafted plan. Its PASS accepts the plan; its
    REWORK sends the plan back to the planner with the critique, within the critique budget –
    counted from the start of the plan's fix round – else a human decides."""
    role: str = "critic"
    statuses: ClassVar = frozenset({REVIEWING})
    waits_in: ClassVar = frozenset()

    def enter(self, active: ActivePhase) -> None:
        active.step.move(REVIEWING)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status == REVIEWING:
            active.step.run(active)

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
            return Escalate("critique_budget", f"G-C2: {cycles} critique cycles without PASS",
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
        active.step.move(DEFINING)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status == DEFINING:
            active.step.run(active)

    def recover(self, active: ActivePhase) -> None:
        self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.type.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        return False

    async def perform(self, active: ActivePhase) -> object:
        return None                                      # its work is the workflow's, under the lock

    async def result(self, active: ActivePhase, done: object) -> PhaseResult:
        node = active.node
        ids = active.step.events.define_tasks(node)
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
        active.step.move(READY if self.at_head else VERIFYING)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, VERIFYING):
            active.step.run(active)

    def recover(self, active: ActivePhase) -> None:
        self.start(active)                                     # verified again

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)                                     # verified again

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.type.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        if active.node.status != READY:
            return False
        active.node.candidate_sha = active.c.change.integration_head      # at the head: what it verifies
        active.step.move(VERIFYING)
        return True

    async def perform(self, active: ActivePhase) -> Verification:
        node = active.node
        c = active.c
        commands = self._to_run(active)
        c.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                f"({'setup and ' if c.change.setup_pin else ''}{len(commands)} command(s))",
                node.id)
        return await verification(active.ctx, node.id, node.candidate_sha, commands,
                                  dispatch=active.dispatch)

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


async def verification(ctx: PhaseContext, label: str, sha: str, commands: tuple[str, ...], *,
                       dispatch: StepDispatcher | Dispatcher) -> Verification:
    """A verification of ``sha``: the repository's setup and ``commands`` in a fresh verifier
    sandbox, as work for ``dispatch`` (a step's). Never raises (other than
    :class:`.Withdrawn`, its step having ended). A name the gateway already has (another
    controller) is replaced and the sandbox created again."""
    c = ctx.c
    name = c.host.new_sandbox_name(VERIFIER_PREFIX)
    if not commands and not c.change.setup_pin:
        return Verification(sha=sha, sandbox=name, infra_error="no commands to verify")
    work = ctx.sandboxes.verifier_work(name, label, sha, commands)
    return verification_of(sha, await dispatch.run(work))


def verification_of(sha: str, outcome: Outcome) -> Verification:
    """The verification record of a verifier's work: the setup's and the commands' results; a
    sandbox that could not be made ready (created, cloned, checked out) is an infrastructure
    error."""
    res = Verification(sha=sha, sandbox=outcome.sandbox,
                       results=outcome.results(SETUP, COMMAND))
    stop = outcome.stopped
    if outcome.error:
        res.infra_error = outcome.error
    elif stop is not None and stop.command.kind == PREPARE:
        res.infra_error = (f"verifier {stop.command.label} failed ({stop.result.exit_code}): "
                           f"{stop.result.output[-1500:]}")
    res.ok = res.infra_error is None and all(r.exit_code == 0 for r in res.results)
    return res


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
        active.step.move(MERGING)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status == MERGING:                  # entered, resumed, overridden, restarted
            active.step.run(active)

    def recover(self, active: ActivePhase) -> None:
        self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.type.rework_resume

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
                attestations=[f"git:{c.cfg.repo.name}@{head}"])], "merge", base=head)
        c.change.integration_head = out
        return Ok(out)


# ---------------------------------------------------------------------------- environment


@dataclass(frozen=True, eq=False)
class BuildImages(Phase):
    """Provisioning, as a step's phase: once its waits hold, the provisioner builds the approved
    environment's images and checks the setup (G-PV1, G-PV3); its result is the phase's."""
    statuses: ClassVar = frozenset({READY, RUNNING})
    waits_in: ClassVar = frozenset({READY})                 # the environment is quiet

    def enter(self, active: ActivePhase) -> None:
        active.step.move(READY)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, RUNNING):
            active.step.run(active)

    def recover(self, active: ActivePhase) -> None:
        self.start(active)                                     # built again

    def resume(self, active: ActivePhase, answered: bool) -> None:
        self.start(active)                                     # built again

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.type.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        if active.node.status != READY:
            return False
        active.step.move(RUNNING)
        active.c.event("provision", "provisioning the approved environment", active.node.id)
        return True

    async def perform(self, active: ActivePhase) -> object:
        """The provisioner's work: the images built and the setup checked (G-PV1, G-PV3)."""
        return await active.step.provisioner.provision(active.node, dispatch=active.dispatch)

    async def result(self, active: ActivePhase, done: object) -> PhaseResult | None:
        """What the provisioner found is the phase's result."""
        return done if active.node.status == RUNNING else None          # type: ignore[return-value]


# ---------------------------------------------------------------------------- gates
class Subjects:
    """What approving a gate means beyond the answer itself: subjects approved separately (and
    still fresh); the gate passes once none is pending."""

    def pending(self, c: ChangeCore) -> list[str]:
        raise NotImplementedError


@dataclass(frozen=True)
class SubjectsApproved(Subjects):
    """H-E1's (the plan's subjects) or H-2's (the oracle's, and at R3 the independent baseline's
    attestation)."""
    which: str                                   # "plan" | "oracle"
    baseline_from: int = 99                      # the tier rank from which the baseline counts

    def pending(self, c: ChangeCore) -> list[str]:
        from ... import risk
        from ...model import ApprovalState
        subjects = (governance.plan_subjects(c) if self.which == "plan"
                    else governance.oracle_subjects(c))
        status = {x.subject: x.status for x in governance.subject_status(c)}
        out = [s for s in subjects if status.get(s) != ApprovalState.APPROVED]
        if c.change.tier and risk.rank(c.change.tier) >= self.baseline_from:
            base = governance.baseline_subject(c)
            if base is not None and base.status != ApprovalState.APPROVED:
                out.append(governance.BASELINE_SUBJECT)
        return out


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
    Answered, it checks again: its subjects all approved (``subjects``) and its checks met, it
    passes; unmet checks escalate again (a re-check is a retry). A new attempt (the stage sent
    back) asks again."""
    subjects: Subjects | None = None
    checks: GateChecks | None = None
    automatic: AutomaticBelow | None = None
    statuses: ClassVar = frozenset({READY, RUNNING})
    waits_in: ClassVar = frozenset()

    def enter(self, active: ActivePhase) -> None:
        active.step.move(READY)
        self.start(active)

    def start(self, active: ActivePhase) -> None:
        if active.node.status in (READY, RUNNING):         # RUNNING: checking (after a restart)
            active.step.run(active)

    def recover(self, active: ActivePhase) -> None:
        self.start(active)

    def resume(self, active: ActivePhase, answered: bool) -> None:
        if answered:
            active.node.approved = True                 # it stays approved in this attempt
        self.start(active)

    def resume_after(self, active: ActivePhase, escalation: Escalate) -> Resume:
        return active.step.type.rework_resume

    def begin(self, active: ActivePhase) -> bool:
        active.step.move(RUNNING)               # checking
        return True

    async def perform(self, active: ActivePhase) -> object:
        return await self.checks.evaluate(active.c) if self.checks else None

    async def result(self, active: ActivePhase, done: object) -> PhaseResult:
        node = active.node
        c = active.c
        unmet = self.checks.unmet(c, node, done) if self.checks else ()
        approved = node.approved or bool(self.automatic and self.automatic(c))
        pending = self.subjects.pending(c) if self.subjects and approved else []
        if not approved:
            ask = f"{node.title}" + (f" (not met yet: {', '.join(unmet)})" if unmet else "")
            return Escalate("awaiting_approval", ask, Resume.READY, expected=True)
        if pending:
            return Escalate("awaiting_approval", "still to approve: " + ", ".join(pending),
                            Resume.READY, expected=True)
        if unmet:
            return Escalate("preconditions_unmet", ", ".join(unmet), Resume.READY, expected=True)
        return Ok()

