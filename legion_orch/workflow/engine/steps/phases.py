"""Orchestrator phases (plan: stages-and-steps.md §2): the controller's work around an agent's, as
small objects that each end in one result – ok, rework (with feedback for the producer) or
escalate (a human decides).

A phase knows three things, and nothing about which kind of node it serves:

* **how it is entered** (``enter``): the node's status while the phase runs;
* **its work** (:meth:`Phase.task`, the same shape for every phase): once it is ``ready`` it
  ``begin``s (its status), ``perform``s its work (through the step's dispatcher, a check) and
  then – under the lock – its ``result`` says what that means;
* **its result**: always ``result`` – pure where the work's record says it all
  (``Verify.interpret``, ``Panel.decide``), the role handler's for an agent, and for a merge
  the guarded merge itself (the guards and the merge hold at once).

What varies between nodes is passed in as small policy objects – which commands to verify
(:class:`CommandSource`), which judges sit on a panel (:class:`JudgeSource`), what a failed
verification means instead of rework – never decided by switching on the node's kind. A step is
an ordered list of phases (:mod:`.steps`); :class:`.StepRunner` moves a node from one phase to
the next.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ....sandbox import VERIFIER_PREFIX
from ... import governance
from ...environment import VERIFIER
from ...model import (
    LIVE_RUN,
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

if TYPE_CHECKING:
    from ..base.dispatch import Dispatcher, StepDispatcher
    from ..sandboxes.sandboxes import Sandboxes
    from ..services.agent_runs import AgentRuns, Ended
    from .runner import StepRunner

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


class Phase:
    """The defaults: entered by becoming READY. ``start`` begins the phase's work as a task
    (:meth:`task`) – on entering, on resuming, and after a restart – which every phase shapes
    the same way through its hooks: ``ready``, ``begin``, ``perform``, ``result``."""
    #: the node statuses while this phase is the node's current one
    statuses: ClassVar[frozenset[NodeStatus]] = frozenset()
    #: the statuses its work is begun in (None: all of its own)
    starts_in: ClassVar[frozenset[NodeStatus] | None] = None

    def enter(self, rt: StepRunner, node: Node) -> None:
        rt.phase(node, READY)
        self.start(rt, node)

    def start(self, rt: StepRunner, node: Node) -> None:
        """Begin the phase's work for the node's status (entered, resumed, or restarted)."""
        if node.status in (self.statuses if self.starts_in is None else self.starts_in):
            rt.run(node, lambda work: self.task(rt, node, work))

    # ------------------------------------------------------------ its work, the same for all
    async def task(self, rt: StepRunner, node: Node, work: StepDispatcher,
                   job: object = None) -> None:
        """The phase's work: once it is :meth:`ready` (at once, or at a later save – the step's
        slot given back meanwhile) it :meth:`begin`s under the lock, :meth:`perform`s its
        work outside it, and then – under the lock, unless its step or phase ended meanwhile –
        :meth:`result` says what the work means, which is concluded. ``job``: which of the
        phase's works this is (a panel's judge, an answered gate)."""
        c = rt.c
        while await rt.wait_until(node, work, lambda: self.ready(rt, node, job)):
            async with c.lock:
                if rt.stale(node, work, self):
                    return
                if self.begin(rt, node, job):
                    c.save()
            done = await self.perform(rt, node, work, job)
            async with c.lock:
                if rt.stale(node, work, self):
                    return
                result = await self.result(rt, node, done, job)
                if result is AGAIN:
                    c.save()
                    continue
                self.conclude(rt, node, result)       # type: ignore[arg-type]
                c.save()
            return

    def ready(self, rt: StepRunner, node: Node, job: object) -> bool:
        """Its work may begin (by default at once)."""
        return True

    def begin(self, rt: StepRunner, node: Node, job: object) -> bool:
        """Under the lock, as its work begins: the node's status. True: it changed (saved)."""
        return False

    async def perform(self, rt: StepRunner, node: Node, work: StepDispatcher,
                      job: object) -> object:
        """The work itself, outside the lock: through the step's dispatcher, or a check."""
        return None

    async def result(self, rt: StepRunner, node: Node, done: object,
                     job: object) -> PhaseResult | None | _Again:
        """Under the lock: what the work (``done``) means – the phase's result (None: it waits
        for more; AGAIN: its work waits again)."""
        return None

    def recover(self, rt: StepRunner, node: Node) -> None:
        """After a restart: take the work up again."""
        self.start(rt, node)

    def resume(self, rt: StepRunner, node: Node, answered: bool) -> None:
        """Its step's escalation was resolved – ``answered`` by a human, or retried – and it
        goes on in this phase."""
        self.start(rt, node)

    def resume_after(self, rt: StepRunner, node: Node, escalation: Escalate) -> Resume:
        """Where the step resumes after an escalation in this phase that did not say (a failed
        run, a BLOCKED report): by default where its step reworks."""
        return rt.step(node).rework_resume

    def conclude(self, rt: StepRunner, node: Node, result: PhaseResult | None) -> None:
        """The phase's result goes to the step runner: ok passes the phase, anything else is
        applied (rework, escalate, an earlier step redone); None waits for more."""
        if isinstance(result, Ok):
            rt.advance(node, self)
        elif result is not None:
            rt.apply(node, result)


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
@dataclass(frozen=True)
class RoleWork:
    """An agent of ``role`` as a phase's work (its ``perform``): whether the step may start it
    (G-B1, the environment), its :class:`.Work` (made by the change's agent runs: the run
    record, the working copy's preparation, the agent's turn), and what it did – the role
    handler's result after an accepted report, or the failure's escalation."""
    role: str

    async def perform(self, rt: StepRunner, node: Node, work: StepDispatcher, phase: Phase, *,
                      wanted: Callable[[], bool], judged_sha: str | None = None,
                      started: Callable[[Run], None] | None = None) -> Ended | _Again | None:
        """Dispatch the agent's work: what it did (the role handler's result after an accepted
        report – read under the lock that ends the run – or the failure's or refusal's
        escalation). AGAIN: when its slot came the step no longer ``wanted`` it (its waits
        stopped holding); None: the step or ``phase`` is over."""
        from ..services.agent_runs import Ended
        c, agents = rt.c, rt.ctx.agents
        async with c.lock:
            if rt.stale(node, work, phase):
                return None
            if refused := agents.preconditions(node, self.role):
                return Ended(None, refused)
        job, prepared = agents.work(
            node, self.role, judged_sha,
            wanted=lambda: not rt.stale(node, work, phase) and wanted(),
            started=started or (lambda run: None))
        outcome = await work.run(job)
        async with c.lock:
            if prepared.run is not None:
                agents.finish(prepared.run, outcome)       # however the work ended
            if outcome.skipped and prepared.refused is None:
                c.save()
                return AGAIN
            if rt.stale(node, work, phase):
                c.save()
                return None
            return Ended(prepared.run, prepared.refused or await agents.conclude(prepared.run))

@dataclass(frozen=True, eq=False)
class Produce(Phase):
    """The step's producer: its work (an agent of a role: ``RoleWork``) dispatched once the
    step's waits hold (and G-H1 for work at the head). Its result – the role handler's, or a
    failure's or refusal's escalation – is the phase's."""
    source: RoleWork
    statuses: ClassVar = frozenset({READY, REWORK, RUNNING})
    starts_in: ClassVar = frozenset({READY, REWORK})        # RUNNING: its agent works

    @property
    def role(self) -> str:
        return self.source.role

    def enter(self, rt: StepRunner, node: Node) -> None:
        if node.status != REWORK:                # a rework continues in REWORK
            rt.phase(node, READY)
        self.start(rt, node)

    def recover(self, rt: StepRunner, node: Node) -> None:
        """After a restart: a run whose agent ended its turn after an accepted report while the
        controller was down is concluded; a waiting step waits again."""
        run = rt.c.runs.get(node.current_run) if node.current_run else None
        if node.status == RUNNING and run is not None and run.status == R_REPORTED \
                and not run.concluded:
            rt.run(node, lambda work: conclude_later(rt, self, node, run, work))
        else:
            self.start(rt, node)

    def ready(self, rt: StepRunner, node: Node, job: object = None) -> bool:
        return node.status in (READY, REWORK) and rt.waits_hold(node) \
            and not rt.provisioner.waits_for_environment(self.role)

    async def perform(self, rt: StepRunner, node: Node, work: StepDispatcher,
                      job: object) -> object:
        c = rt.c
        if rt.step(node).judges_head:            # G-H1: the head is where the controller left it
            head = await c.git.integration_head(c.change.id)
            async with c.lock:
                if moved := c.head_moved(head):
                    return Escalate("head_moved", moved, Resume.READY)
        return await self.source.perform(
            rt, node, work, self, wanted=lambda: self.ready(rt, node),
            started=lambda run: rt.phase(node, RUNNING))        # its agent works

    async def result(self, rt: StepRunner, node: Node, done: object,
                     job: object) -> PhaseResult | None | _Again:
        if done is None or done is AGAIN or isinstance(done, Escalate):
            return done                                          # type: ignore[return-value]
        return self.ended(rt, node, done)                        # type: ignore[arg-type]

    def ended(self, rt: StepRunner, node: Node, ended: Ended) -> PhaseResult | None:
        """The agent's turn ended: its result is the phase's."""
        if ended.run is not None:
            ended.run.concluded = True
        return ended.result

def combined(held: dict[str, Escalate]) -> Escalate:
    """The judges' escalations as one: the first's, asking about each."""
    first = next(iter(held.values()))
    if len(held) == 1:
        return first
    import dataclasses
    return dataclasses.replace(first, question="; ".join(
        f"{role}: {e.question}" for role, e in held.items()))


async def conclude_later(rt: StepRunner, phase: Produce | Panel, node: Node, run: Run,
                         work: StepDispatcher) -> None:
    """After a restart: what an agent's accepted report means, to its phase."""
    from ..services.agent_runs import Ended
    async with rt.c.lock:
        if rt.stale(node, work, phase) or run.concluded or run.status != R_REPORTED:
            return
        phase.conclude(rt, node, phase.ended(rt, node, Ended(run, await rt.ctx.agents.conclude(run))))
        rt.c.save()


@dataclass(frozen=True, eq=False)
class Panel(Phase):
    """The judges, one run per role at the candidate. Unanimous PASS at the candidate is ok;
    otherwise rework with the HIGH findings – or, when the same HIGH findings came back two
    cycles running, escalate (G-RV5). ``decides``: False when a judge's own handler decides
    (the plan's critic, G-C2)."""
    judges: JudgeSource | tuple[str, ...]
    decides: bool = True
    statuses: ClassVar = frozenset({REVIEWING})

    def roles(self, c: ChangeCore | None = None) -> tuple[str, ...]:
        return _judges(self.judges, c)

    def missing(self, node: Node, c: ChangeCore | None = None) -> list[str]:
        return [r for r in self.roles(c) if node.review(node.cycle, r) is None]

    def enter(self, rt: StepRunner, node: Node) -> None:
        roles = self.roles(rt.c)
        if not roles:                        # an empty panel: nothing to wait for
            rt.advance(node, self)
            return
        rt.phase(node, REVIEWING)
        rt.c.event("review", f"panel {list(roles)}", node.id)
        self.start(rt, node)

    def resume_after(self, rt: StepRunner, node: Node, escalation: Escalate) -> Resume:
        """A judge's failed run: the panel again (its other judges' reviews stand); a judge's
        BLOCKED report: where the step reworks."""
        return Resume.REVIEWING if escalation.kind == "run_failed" else rt.step(node).rework_resume

    def _latest(self, c: ChangeCore, node: Node, role: str) -> Run | None:
        """The role's latest run at the node's cycle."""
        runs = [r for r in c.runs.values()
                if r.node_id == node.id and r.role == role and r.cycle == node.cycle]
        return runs[-1] if runs else None

    def _busy(self, c: ChangeCore, node: Node, role: str) -> bool:
        """The judge works, or its turn ended and what it reported is still to be read."""
        run = self._latest(c, node, role)
        return run is not None and (run.status in LIVE_RUN
                                    or (run.status == R_REPORTED and not run.concluded))

    def outstanding(self, c: ChangeCore, node: Node, held: dict) -> list[str]:
        """The judges still to finish this cycle: no review, no escalation held, and their
        work under way or still to start."""
        return [role for role in self.roles(c)
                if node.review(node.cycle, role) is None and role not in held
                and (self._latest(c, node, role) is None or self._busy(c, node, role))]

    def start(self, rt: StepRunner, node: Node) -> None:
        """The missing judges' runs at the candidate (one per role and cycle), in parallel."""
        if node.status != REVIEWING:
            return
        for role in self.roles(rt.c):
            if f"{node.cycle}:{role}" not in node.reviews and not self._busy(rt.c, node, role):
                rt.run(node, lambda work, role=role: self.task(rt, node, work, (role, node.cycle)))

    def recover(self, rt: StepRunner, node: Node) -> None:
        for r in [r for r in rt.c.runs.values() if r.node_id == node.id and r.cycle == node.cycle
                  and r.role in self.roles(rt.c) and r.status == R_REPORTED and not r.concluded]:
            rt.run(node, lambda work, r=r: conclude_later(rt, self, node, r, work))
        self.start(rt, node)

    def wanted(self, rt: StepRunner, node: Node, role: str, cycle: int) -> bool:
        return node.status == REVIEWING and node.cycle == cycle \
            and f"{cycle}:{role}" not in node.reviews

    # a judge's work: ``job`` is (its role, the cycle it judges)
    def ready(self, rt: StepRunner, node: Node, job: object) -> bool:
        role, cycle = job                                        # type: ignore[misc]
        return self.wanted(rt, node, role, cycle) \
            and not rt.provisioner.waits_for_environment(role)

    async def perform(self, rt: StepRunner, node: Node, work: StepDispatcher,
                      job: object) -> object:
        return await RoleWork(job[0]).perform(                    # type: ignore[index]
            rt, node, work, self, wanted=lambda: self.ready(rt, node, job),
            judged_sha=node.candidate_sha)

    async def result(self, rt: StepRunner, node: Node, done: object,
                     job: object) -> PhaseResult | None | _Again:
        role, cycle = job                                        # type: ignore[misc]
        if done is None or done is AGAIN or node.cycle != cycle:
            return done if done is AGAIN else None
        return self.ended(rt, node, done, role)                  # type: ignore[arg-type]

    def ended(self, rt: StepRunner, node: Node, ended: Ended,
              role: str | None = None) -> PhaseResult | None:
        """A judge's turn ended: its run is done and its sandbox retired. The panel waits for
        every judge of the cycle: a judge's escalation (its failure, a BLOCKED report, a refused
        dispatch) is held until the others have finished too, then the panel escalates with all
        of them; otherwise it decides once every judge has reported – or, where a judge's own
        handler decides (the critic), its result does."""
        run, result = ended.run, ended.result
        role = role or (run.role if run else "")
        if run is not None:
            run.concluded = True
            if run.status == R_REPORTED and not isinstance(result, Escalate):
                run.status = R_ACCEPTED
                rt.lifecycle.sandboxes.delete_later(run.sandbox)
        if not self.decides:
            return result
        held = rt.held(node)
        if isinstance(result, Escalate):
            held[role] = result
        if self.outstanding(rt.c, node, held):
            return None                          # the other judges first
        rt.held(node, clear=True)
        return combined(held) if held else self.decide(node, rt.c)

    def decide(self, node: Node, c: ChangeCore | None = None) -> PhaseResult | None:
        """None until every judge has reported (records the cycle's finding fingerprints)."""
        results = [node.review(node.cycle, r) for r in self.roles(c)]
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
    @property
    def statuses(self) -> frozenset[NodeStatus]:  # type: ignore[override]
        # at the head (no producer): READY until dispatched, then VERIFYING
        return frozenset({READY, VERIFYING}) if self.at_head else frozenset({VERIFYING})

    def commands_for(self, c: ChangeCore | None, node: Node | None) -> tuple[str, ...]:
        if isinstance(self.commands, tuple):
            return self.commands
        return tuple(self.commands(c, node))

    @staticmethod
    def record(node: Node) -> tuple[str, str]:
        """Where the verification is stored, and its key on the node: a task's per cycle."""
        if node.task:
            return f"tasks/{node.task.id}/verification.json", str(node.cycle)
        return f"control/verification/{_slug(node.id)}.json", str(node.kind)

    def enter(self, rt: StepRunner, node: Node) -> None:
        """After a producer, its candidate is verified at once (in the step's slot); at the head
        (nothing produced it) the step starts READY, like any step's first work."""
        rt.phase(node, READY if self.at_head else VERIFYING)
        self.start(rt, node)

    def ready(self, rt: StepRunner, node: Node, job: object) -> bool:
        """At the head: once its waits hold. A verifier sandbox comes from the build: none
        while it is built again."""
        return (node.status != READY or rt.waits_hold(node)) \
            and not rt.provisioner.waits_for_environment(VERIFIER)

    def begin(self, rt: StepRunner, node: Node, job: object) -> bool:
        if node.status != READY:
            return False
        node.candidate_sha = rt.c.change.integration_head      # at the head: what it verifies
        rt.phase(node, VERIFYING)
        return True

    async def perform(self, rt: StepRunner, node: Node, work: StepDispatcher,
                      job: object) -> Verification:
        c = rt.c
        commands = self.commands_for(c, node)
        c.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                f"({'setup and ' if c.change.setup_pin else ''}{len(commands)} command(s))",
                node.id)
        return await self.verification(rt.ctx, node.id, node.candidate_sha, commands,
                                       dispatch=work)

    async def result(self, rt: StepRunner, node: Node, done: object,
                     job: object) -> PhaseResult | None:
        """The verification is recorded, then interpreted."""
        if node.status != VERIFYING:
            return None
        res: Verification = done                                 # type: ignore[assignment]
        path, key = self.record(node)
        att = rt.c.write_record(path, res).attestation
        # The node keeps a compact copy (the stored record has the command output).
        node.verification[key] = res.model_copy(update={
            "attestation": att,
            "results": [r.model_copy(update={"output": ""}) for r in res.results]})
        return self.verified(rt, node, res, att)

    async def verification(self, ctx: PhaseContext, label: str, sha: str,
                           commands: tuple[str, ...] | None = None, *,
                           dispatch: StepDispatcher | Dispatcher) -> Verification:
        """Run it, as work for ``dispatch`` (the step's).
        Never raises (other than :class:`.Withdrawn`, its step having ended). A name the gateway
        already has (another controller) is replaced and the sandbox created again."""
        c = ctx.c
        commands = self.commands_for(None, None) if commands is None else commands
        name = c.host.new_sandbox_name(VERIFIER_PREFIX)
        if not commands and not c.change.setup_pin:
            return Verification(sha=sha, sandbox=name, infra_error="no commands to verify")
        work = ctx.sandboxes.verifier_work(name, label, sha, commands)
        return verification_of(sha, await dispatch.run(work))

    def interpret(self, res: Verification, cycle: int, att: str | None) -> PhaseResult:
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

    def verified(self, rt: StepRunner, node: Node, res: Verification, att: str) -> PhaseResult:
        """Under the lock, once the verification is recorded: the phase's result for the node
        (a failed setup: what the policy makes of it)."""
        result = self.interpret(res, node.cycle, att)
        if isinstance(result, Ok):
            for kind, text in self.notes(rt.c, node, res) if self.notes else []:
                rt.c.event(kind, text, node.id)
        elif isinstance(result, Escalate) and result.kind == "setup_failed" \
                and self.on_setup_failed:
            result = self.on_setup_failed(rt.c, node, setup_failure(res), att, result)
        return result


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
    """G-A1 (verification and every panel PASS at the candidate, unless overridden), G-H1 (the
    integration head is where the controller left it), then the merge into the integration
    branch; a conflict is rework onto the new head (G-A2). Under the lock. Ok: the new head.
    The judges whose PASS it requires are its step's panel's."""
    statuses: ClassVar = frozenset({MERGING})

    def enter(self, rt: StepRunner, node: Node) -> None:
        rt.phase(node, MERGING)
        self.start(rt, node)

    async def result(self, rt: StepRunner, node: Node, done: object,
                     job: object) -> PhaseResult:
        """It has no work outside the lock: its guards and the merge hold at once."""
        from ..services import cleanup
        c = rt.c
        sha = node.candidate_sha
        result = await self.run(rt.ctx, node, rt.step(node).judges(c))
        if isinstance(result, Ok):
            node.accepted_sha = sha
            run = c.runs.get(node.current_run) if node.current_run else None
            await cleanup.delete_run_branch(c, run.branch if run else None, node.id)
            c.event("accepted", f"merged {sha[:12]} -> integration {result.value[:12]}",
                    node.id)
        return result

    async def run(self, ctx: PhaseContext, node: Node, judges: tuple[str, ...]) -> PhaseResult:
        c = ctx.c
        sha = node.candidate_sha
        ver = node.current_verification()
        v = []
        if not ver or not ver.ok or ver.sha != sha:
            v.append("G-A1: no passing deterministic verification at candidate")
        if not node.override:
            for role in judges:
                r = node.review(node.cycle, role)
                if not r or r.verdict != Verdict.PASS or r.sha != sha:
                    v.append(f"G-A1: no {role} PASS at candidate")
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
@dataclass(eq=False)
class BuildImages(Phase):
    """Provisioning, as a step's phase: once its waits hold, the provisioner builds the approved
    environment's images and checks the setup (G-PV1, G-PV3); its result is the phase's."""
    statuses: ClassVar = frozenset({READY, RUNNING})

    def ready(self, rt: StepRunner, node: Node, job: object) -> bool:
        return node.status != READY or rt.waits_hold(node)

    def begin(self, rt: StepRunner, node: Node, job: object) -> bool:
        if node.status != READY:
            return False
        rt.phase(node, RUNNING)
        rt.c.event("provision", "provisioning the approved environment", node.id)
        return True

    async def perform(self, rt: StepRunner, node: Node, work: StepDispatcher,
                      job: object) -> object:
        """The provisioner's work: the images built and the setup checked (G-PV1, G-PV3)."""
        return await rt.provisioner.provision(node, dispatch=work)

    async def result(self, rt: StepRunner, node: Node, done: object,
                     job: object) -> PhaseResult | None:
        """What the provisioner found is the phase's result."""
        return done if node.status == RUNNING else None          # type: ignore[return-value]

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

    def enter(self, rt: StepRunner, node: Node) -> None:
        rt.phase(node, READY)
        self.start(rt, node)

    def resume(self, rt: StepRunner, node: Node, answered: bool) -> None:
        rt.run(node, lambda work: self.task(rt, node, work, answered))

    # its work: ``job`` is whether a human answered it (approved it)
    def begin(self, rt: StepRunner, node: Node, job: object) -> bool:
        rt.phase(node, RUNNING)               # checking
        return True

    async def perform(self, rt: StepRunner, node: Node, work: StepDispatcher,
                      job: object) -> object:
        return await self.checks.evaluate(rt.c) if self.checks else None

    async def result(self, rt: StepRunner, node: Node, done: object,
                     job: object) -> PhaseResult:
        c = rt.c
        unmet = self.checks.unmet(c, node, done) if self.checks else ()
        node.approved = node.approved or bool(job)        # it stays approved in this attempt
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
