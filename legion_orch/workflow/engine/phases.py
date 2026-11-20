"""Orchestrator phases (plan: stages-and-steps.md §2): the controller's work around an agent's, as
small objects that each end in one result – ok, rework (with feedback for the producer) or
escalate (a human decides).

A phase knows three things, and nothing about which kind of node it serves:

* **its work and result** – ``Verify.judge``, ``Panel.decide``, ``Merge.run`` …;
* **how it is entered** (``enter``): the node's status while the phase runs;
* **its work** (``start``): a task that awaits what it needs – its step's waits, a slot from
  the step's dispatcher – runs, and hands its result to the step runner.

What varies between nodes is passed in as small policy objects – which commands to verify
(:class:`CommandSource`), which judges sit on a panel (:class:`JudgeSource`), what a failed
verification means instead of rework – never decided by switching on the node's kind. A step is
an ordered list of phases (:mod:`.steps`); :class:`.StepRunner` moves a node from one phase to
the next.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from ...sandbox import VERIFIER_PREFIX, BuildError, ImageRecipe, is_name_clash
from .. import governance
from ..model import (
    AWAITING_HUMAN,
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
from ..results import Escalate, Ok, PhaseResult, Redo, Rework
from ..ship import evaluate
from .common import _slug
from .core import ChangeCore
from .workflow_tree import in_round

if TYPE_CHECKING:
    from .agent_runs import AgentRuns, Ended
    from .dispatch import Dispatcher, StepDispatcher
    from .sandboxes import Sandboxes
    from .steps import StepRunner

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


class Phase:
    """The defaults: entered by becoming READY, no work of its own. ``start`` begins the phase's
    work as a task (:meth:`.StepRunner.run`) – on entering, on resuming, and after a restart –
    which awaits what it needs, dispatches its work through the step's dispatcher, and gives
    its result to :meth:`conclude`."""
    #: the node statuses while this phase is the node's current one
    statuses: ClassVar[frozenset[NodeStatus]] = frozenset()

    def enter(self, rt: StepRunner, node: Node, after: Phase | None) -> None:
        rt.phase(node, READY)
        self.start(rt, node)

    def start(self, rt: StepRunner, node: Node) -> None:
        """Begin the phase's work for the node's status (entered, resumed, or restarted)."""

    def recover(self, rt: StepRunner, node: Node) -> None:
        """After a restart: take the work up again."""
        self.start(rt, node)

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
@dataclass(frozen=True, eq=False)
class AgentRun(Phase):
    """The role's agent for the node (guards G-D1..G-D4, G-B1, G-H1), as work dispatched once
    the step's waits hold. Its result – the role handler's, after the agent's turn ended with
    an accepted report, or the failure's escalation – is the phase's."""
    role: str
    statuses: ClassVar = frozenset({READY, REWORK, RUNNING})

    def enter(self, rt: StepRunner, node: Node, after: Phase | None) -> None:
        if node.status != REWORK:                # a rework continues in REWORK
            rt.phase(node, READY)
        self.start(rt, node)

    def start(self, rt: StepRunner, node: Node) -> None:
        if node.status in (READY, REWORK):
            rt.run(node, lambda work: self._work(rt, node, work))

    def recover(self, rt: StepRunner, node: Node) -> None:
        """After a restart: a run whose agent ended its turn after an accepted report while the
        controller was down is concluded; a waiting step waits again."""
        run = rt.c.runs.get(node.current_run) if node.current_run else None
        if node.status == RUNNING and run is not None and run.status == R_REPORTED \
                and not run.concluded:
            rt.run(node, lambda work: conclude_later(rt, self, node, run, work))
        else:
            self.start(rt, node)

    def ready(self, rt: StepRunner, node: Node) -> bool:
        return node.status in (READY, REWORK) and rt.waits_hold(node) \
            and not rt.provisioner.waits_for_environment(self.role)

    async def _work(self, rt: StepRunner, node: Node, work: StepDispatcher) -> None:
        from .agent_runs import AgentWork
        c = rt.c
        while await rt.wait_until(node, work, lambda: self.ready(rt, node)):
            if rt.step(node).judges_head:        # G-H1: the head is where the controller left it
                head = await c.git.integration_head(c.change.id)
                async with c.lock:
                    if rt.stale(node, work, self):
                        return
                    if moved := c.head_moved(head):
                        rt.apply(node, Escalate("head_moved", moved, Resume.READY))
                        c.save()
                        return
            ended = await work.run(AgentWork(
                rt.ctx.agents, node, self.role,
                still=lambda: not rt.stale(node, work, self) and self.ready(rt, node),
                started=lambda run: rt.phase(node, RUNNING)))       # its agent works
            if ended is not None:
                async with c.lock:
                    if not rt.stale(node, work, self):
                        self.ended(rt, node, ended)
                        c.save()
                return
            # its waits no longer held when its slot came: it waits again

    def ended(self, rt: StepRunner, node: Node, ended: Ended) -> None:
        """The agent's turn ended: its result is the phase's."""
        if ended.run is not None:
            ended.run.concluded = True
        self.conclude(rt, node, ended.result)


async def conclude_later(rt: StepRunner, phase: AgentRun | Panel, node: Node, run: Run,
                         work: StepDispatcher) -> None:
    """After a restart: what an agent's accepted report means, to its phase."""
    from .agent_runs import Ended
    async with rt.c.lock:
        if rt.stale(node, work, phase) or run.concluded or run.status != R_REPORTED:
            return
        result = await rt.ctx.agents.reports.conclude(run)
        phase.ended(rt, node, Ended(run, result))
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

    def enter(self, rt: StepRunner, node: Node, after: Phase | None) -> None:
        roles = self.roles(rt.c)
        if not roles:                        # an empty panel: nothing to wait for
            rt.advance(node, self)
            return
        rt.phase(node, REVIEWING)
        if isinstance(after, Verify):
            rt.c.event("review", f"verification passed; panel {list(roles)}", node.id)
        self.start(rt, node)

    def _busy(self, c: ChangeCore, node: Node, role: str) -> bool:
        return any(r.node_id == node.id and r.role == role and r.cycle == node.cycle
                   and r.status in LIVE_RUN | {R_REPORTED} for r in c.runs.values())

    def start(self, rt: StepRunner, node: Node) -> None:
        """The missing judges' runs at the candidate (one per role and cycle), in parallel."""
        if node.status != REVIEWING:
            return
        for role in self.roles(rt.c):
            if f"{node.cycle}:{role}" not in node.reviews and not self._busy(rt.c, node, role):
                rt.run(node, lambda work, role=role: self._judge(rt, node, role, work))

    def recover(self, rt: StepRunner, node: Node) -> None:
        for r in [r for r in rt.c.runs.values() if r.node_id == node.id and r.cycle == node.cycle
                  and r.role in self.roles(rt.c) and r.status == R_REPORTED and not r.concluded]:
            rt.run(node, lambda work, r=r: conclude_later(rt, self, node, r, work))
        self.start(rt, node)

    def wanted(self, rt: StepRunner, node: Node, role: str, cycle: int) -> bool:
        return node.status == REVIEWING and node.cycle == cycle \
            and f"{cycle}:{role}" not in node.reviews

    async def _judge(self, rt: StepRunner, node: Node, role: str, work: StepDispatcher) -> None:
        from .agent_runs import AgentWork
        c, cycle = rt.c, node.cycle
        while await rt.wait_until(node, work, lambda: self.wanted(rt, node, role, cycle) and
                                  not rt.provisioner.waits_for_environment(role)):
            ended = await work.run(AgentWork(
                rt.ctx.agents, node, role, judged_sha=node.candidate_sha,
                still=lambda: not rt.stale(node, work, self) and node.cycle == cycle
                and self.wanted(rt, node, role, cycle)
                and not rt.provisioner.waits_for_environment(role)))
            if ended is not None:
                async with c.lock:
                    if not rt.stale(node, work, self) and node.cycle == cycle:
                        self.ended(rt, node, ended)
                        c.save()
                return
            if not self.wanted(rt, node, role, cycle):
                return

    def ended(self, rt: StepRunner, node: Node, ended: Ended) -> None:
        """A judge's turn ended: its run is done and its sandbox retired. An escalation (its
        failure, or its handler's) stands; otherwise the panel decides once every judge has
        reported – or, where a judge's own handler decides (the critic), its result does."""
        run, result = ended.run, ended.result
        if run is not None:
            run.concluded = True
            if run.status == R_REPORTED:
                run.status = R_ACCEPTED
                rt.lifecycle.sandboxes.delete_later(run.sandbox)
        if self.decides and not isinstance(result, Escalate):
            result = self.decide(node, rt.c)
        self.conclude(rt, node, result)

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

    def enter(self, rt: StepRunner, node: Node, after: Phase | None) -> None:
        """After a producer, its candidate is verified at once (in the step's slot); at the head
        (nothing produced it) the step starts READY, like any step's first work."""
        rt.phase(node, READY if self.at_head else VERIFYING)
        self.start(rt, node)

    def start(self, rt: StepRunner, node: Node) -> None:
        if node.status in (READY, VERIFYING):
            rt.run(node, lambda work: self._work(rt, node, work))

    async def _work(self, rt: StepRunner, node: Node, work: StepDispatcher) -> None:
        c = rt.c
        if node.status == READY:                 # at the head: once its waits hold
            if not await rt.wait_until(node, work, lambda: rt.waits_hold(node)):
                return
            async with c.lock:
                if rt.stale(node, work, self):
                    return
                node.candidate_sha = c.change.integration_head
                rt.phase(node, VERIFYING)
                c.save()
        commands = self.commands_for(c, node)
        c.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                f"({'setup and ' if c.change.setup_pin else ''}{len(commands)} command(s))",
                node.id)
        res = await self.verification(rt.ctx, node.id, node.candidate_sha, commands,
                                      dispatch=work)
        async with c.lock:
            if rt.stale(node, work, self) or node.status != VERIFYING:
                return
            path, key = self.record(node)
            att = c.write_record(path, res).attestation
            # The node keeps a compact copy (the stored record has the command output).
            node.verification[key] = res.model_copy(update={
                "attestation": att,
                "results": [r.model_copy(update={"output": ""}) for r in res.results]})
            self.verified(rt, node, res, att)
            c.save()

    async def verification(self, ctx: PhaseContext, label: str, sha: str,
                           commands: tuple[str, ...] | None = None, *,
                           dispatch: StepDispatcher | Dispatcher | None = None) -> Verification:
        """Run it, as work for the dispatcher (``dispatch``: a step's, else the shared one).
        Never raises (other than :class:`.Withdrawn`, its step having ended). A name the gateway
        already has (another controller) is replaced and the sandbox created again."""
        c = ctx.c
        commands = self.commands_for(None, None) if commands is None else commands
        result = Verification(sha=sha, sandbox=c.host.new_sandbox_name(VERIFIER_PREFIX))
        if not commands and not c.change.setup_pin:
            result.infra_error = "no commands to verify"
            return result
        return await (dispatch or ctx.dispatcher).run(
            VerifierWork(self, ctx, result, label, sha, commands))

    async def _run(self, ctx: PhaseContext, result: Verification, label: str, sha: str,
                   commands: tuple[str, ...]) -> Verification:
        c = ctx.c
        for attempt in range(3):
            try:
                async with ctx.sandboxes.verifier(result.sandbox, label, sha) as box:
                    pin = c.change.setup_pin
                    if pin:                    # §5.3c, G-V3: setup first; a failure stops here
                        r = await box.setup(pin.blob, c.cfg.limits.setup_timeout_s)
                        result.results.append(r)
                    if not pin or result.results[-1].exit_code == 0:
                        for cmd in commands:
                            result.results.append(await box.run(cmd))
                result.ok = all(r.exit_code == 0 for r in result.results)
            except Exception as e:
                if is_name_clash(e) and not result.results and attempt < 2:
                    result.sandbox = c.host.new_sandbox_name(VERIFIER_PREFIX)
                    continue
                result.infra_error = str(e)
            return result
        return result

    def judge(self, res: Verification, cycle: int, att: str | None) -> PhaseResult:
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

    def verified(self, rt: StepRunner, node: Node, res: Verification, att: str) -> None:
        """Under the lock, once the verification is recorded: the phase's result for the node
        (a failed setup: what the policy makes of it)."""
        result = self.judge(res, node.cycle, att)
        if isinstance(result, Ok):
            for kind, text in self.notes(rt.c, node, res) if self.notes else []:
                rt.c.event(kind, text, node.id)
        elif isinstance(result, Escalate) and result.kind == "setup_failed" \
                and self.on_setup_failed:
            result = self.on_setup_failed(rt.c, node, setup_failure(res), att, result)
        self.conclude(rt, node, result)


@dataclass
class VerifierWork:
    """A verification as work for the dispatcher: the commands in a fresh verifier sandbox."""
    phase: Verify
    ctx: PhaseContext
    result: Verification
    label: str
    sha: str
    commands: tuple[str, ...]

    async def run(self) -> Verification:
        return await self.phase._run(self.ctx, self.result, self.label, self.sha, self.commands)


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
    branch; a conflict is rework onto the new head (G-A2). Under the lock. Ok: the new head."""
    judges: JudgeSource | tuple[str, ...] = ()
    statuses: ClassVar = frozenset({MERGING})

    def enter(self, rt: StepRunner, node: Node, after: Phase | None) -> None:
        rt.phase(node, MERGING)
        self.start(rt, node)

    def start(self, rt: StepRunner, node: Node) -> None:
        """Merge (entered, resumed, overridden, or after a restart)."""
        if node.status == MERGING:
            rt.run(node, lambda work: self._work(rt, node, work))

    async def _work(self, rt: StepRunner, node: Node, work: StepDispatcher) -> None:
        from . import cleanup
        c = rt.c
        async with c.lock:
            if rt.stale(node, work, self):
                return
            sha = node.candidate_sha
            result = await self.run(rt.ctx, node)
            if isinstance(result, Ok):
                node.accepted_sha = sha
                run = c.runs.get(node.current_run) if node.current_run else None
                await cleanup.delete_run_branch(c, run.branch if run else None, node.id)
                c.event("accepted", f"merged {sha[:12]} -> integration {result.value[:12]}",
                        node.id)
            self.conclude(rt, node, result)
            c.save()

    async def run(self, ctx: PhaseContext, node: Node) -> PhaseResult:
        c = ctx.c
        sha = node.candidate_sha
        ver = node.current_verification()
        v = []
        if not ver or not ver.ok or ver.sha != sha:
            v.append("G-A1: no passing deterministic verification at candidate")
        if not node.override:
            for role in _judges(self.judges, c):
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
    """Build (or find) the image of each recipe. Ok: recipe tag -> image reference. ``log``
    keeps what was built, for the provisioning record. As a step's phase: provisioning, once
    dispatched (the provisioner resolves the recipes and checks the setup, G-PV3)."""
    recipes: dict[str, ImageRecipe] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    statuses: ClassVar = frozenset({READY, RUNNING})

    def start(self, rt: StepRunner, node: Node) -> None:
        if node.status in (READY, RUNNING):
            rt.run(node, lambda work: self._provision(rt, node, work))

    async def _provision(self, rt: StepRunner, node: Node, work: StepDispatcher) -> None:
        """Once its waits hold: the provisioner's work, and its result to the step runner."""
        c = rt.c
        if node.status == READY:
            if not await rt.wait_until(node, work, lambda: rt.waits_hold(node)):
                return
            async with c.lock:
                if rt.stale(node, work, self):
                    return
                rt.phase(node, RUNNING)
                c.event("provision", "provisioning the approved environment", node.id)
                c.save()
        result = await rt.provisioner.provision(node, dispatch=work)
        async with c.lock:
            if result is not None and not rt.stale(node, work, self) and node.status == RUNNING:
                self.conclude(rt, node, result)
                c.save()

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        images: dict[str, str | None] = {}
        try:
            for tag, recipe in self.recipes.items():
                self.log.append(f"## image {tag}\n\n{recipe.files['Dockerfile']}")
                images[tag], out = await ctx.c.svc.builder.ensure_image(recipe)
                self.log.append(out)
        except (BuildError, OSError) as e:
            return Escalate("provision_failed", f"G-PV1: {e}"[:2000], Resume.READY,
                            log_tail="\n".join(self.log)[-4000:] or None)
        return Ok(images)


# ---------------------------------------------------------------------------- checks, humans
@dataclass(frozen=True, eq=False)
class Evaluate(Phase):
    """Deterministic checks, no sandbox: the ship gates (workflow/ship.py). Always Ok with the
    readiness: a NOT_READY verdict is a finding for the gate that reads it (H-3's
    preconditions, :mod:`.runtime`), not a failure."""

    async def run(self, ctx: PhaseContext) -> Ok:
        result: Readiness = await evaluate(ctx.c)
        return Ok(result)


@dataclass(frozen=True, eq=False)
class Approval(Phase):
    """A human decides (a gate node, see :mod:`.human`)."""
    statuses: ClassVar = frozenset({AWAITING_HUMAN})

    def enter(self, rt: StepRunner, node: Node, after: Phase | None) -> None:
        rt.runtime.gates.gate(node, AWAITING_HUMAN)     # a human's decision: no work
        rt.c.event("human", "awaiting human decision", node.id)

