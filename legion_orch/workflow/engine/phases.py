"""Orchestrator phases (plan: stages-and-steps.md §2): the controller's work around an agent's, as
small objects that each end in one result – ok, rework (with feedback for the producer) or
escalate (a human decides).

A phase knows three things, and nothing about which kind of node it serves:

* **its work and result** – ``Verify.judge``, ``Panel.decide``, ``Merge.run`` …;
* **how it is entered** (``enter``): the node's status while the phase runs, and the work spawned;
* **how it is dispatched** (``dispatch``, for phases that wait their turn: an agent run, a build).

What varies between nodes is passed in as small policy objects – which commands to verify
(:class:`CommandSource`), which judges sit on a panel (:class:`JudgeSource`), what a failed
verification means instead of rework – never decided by switching on the node's kind. A step is
an ordered list of phases (:mod:`.steps`); :class:`.StepRunner` moves a node from one phase to
the next.
"""
from __future__ import annotations

from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from ...sandbox import VERIFIER_PREFIX, BuildError, ImageRecipe, is_name_clash
from .. import governance
from ..model import (
    AWAITING_HUMAN,
    LIVE_RUN,
    MERGING,
    R_ACCEPTED,
    R_REPORTED,
    R_SUPERSEDED,
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
from ..results import Escalate, Ok, PhaseResult, Rework
from ..ship import evaluate
from .common import _slug
from .core import ChangeCore

if TYPE_CHECKING:
    from .acceptance import Acceptance
    from .dispatch import Dispatcher
    from .sandboxes import Sandboxes
    from .steps import StepRunner

EXIT_NOT_FOUND = 127   # bash: command not found (126, not executable, is often a fixable repo file)


# the results every phase ends in, an agent's included (workflow/results.py)
__all__ = ["Escalate", "Ok", "PhaseResult", "Rework"]


@dataclass
class PhaseContext:
    """What phases work with: the change, its sandboxes, and (for agent runs) the dispatcher."""
    c: ChangeCore
    sandboxes: Sandboxes
    dispatcher: Dispatcher | None = None

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
            c.escalate(node, "cycle_budget",
                       f"G-RV5: {node.cycle} cycle(s) used without acceptance ({source})",
                       resume=Resume.REWORK_CYCLE)
            return
        node.cycle += 1
        node.candidate_sha = None
        node.status = REWORK
        c.event("rework", f"{source}: rework cycle {node.cycle}", node.id)

    def apply(self, node: Node, result: Rework | Escalate) -> None:
        """Under the lock: a rework or an escalation becomes the node's state."""
        if isinstance(result, Rework):
            self.rework(node, result.feedback, result.source, result.budget_checked)
            if result.base and node.status == REWORK:
                node.base_sha = result.base
        else:
            node.feedback += result.feedback
            self.c.escalate(node, result.kind, result.question, resume=result.resume,
                            log_tail=result.log_tail, run=result.run,
                            missing_decision=result.missing_decision)


class Phase:
    """The defaults: entered by becoming READY, nothing to dispatch, nothing to resume.
    Entering only sets the node's status (or, after a producer, starts the controller's own
    work on its result); work that has to wait its turn starts in ``dispatch``."""
    #: the node statuses while this phase is the node's current one
    statuses: ClassVar[frozenset[NodeStatus]] = frozenset()

    def enter(self, rt: StepRunner, node: Node, after: Phase | None) -> None:
        node.status = READY

    def dispatch(self, rt: StepRunner, node: Node, capacity: int) -> list[Run]:
        return []

    def resume(self, rt: StepRunner, node: Node) -> Coroutine[Any, Any, None] | None:
        """The controller's work to start again after a restart, while the node is RUNNING."""
        return None

    def reported(self, rt: StepRunner, node: Node, run: Run, result: PhaseResult | None) -> None:
        """An agent's report in this phase was accepted; its role handler says what it means:
        ok passes the phase, rework or escalate is applied, None waits for more."""
        if isinstance(result, Ok):
            rt.advance(node, self)
        elif result is not None:
            rt.ctx.apply(node, result)


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

    def __call__(self, a: Acceptance, node: Node, setup: CommandResult, att: str,
                 escalation: Escalate) -> None:
        raise NotImplementedError


class OracleReworks(SetupFailurePolicy):
    """The oracle's author fixes it within its cycle budget (a human decides after)."""

    def __call__(self, a: Acceptance, node: Node, setup: CommandResult, att: str,
                 escalation: Escalate) -> None:
        a.setup_failed(node, setup, att, escalation)


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
    """Dispatch the role's agent for the node (guards G-D1..G-D4, G-B1, G-H1). The run ends
    with a report; the role's handler records what it says and returns the phase's result
    (:meth:`Phase.reported`)."""
    role: str
    statuses: ClassVar = frozenset({READY, REWORK, RUNNING})

    def start(self, ctx: PhaseContext, node: Node, judged_sha: str | None = None) -> Run | None:
        """Prepare the run (launched outside the lock), or None if a guard stopped it."""
        assert ctx.dispatcher is not None
        return ctx.dispatcher.prepare(node, self.role, judged_sha=judged_sha)

    def dispatch(self, rt: StepRunner, node: Node, capacity: int) -> list[Run]:
        s, c, step = rt.scheduler, rt.c, rt.step(node)
        if node.status not in (READY, REWORK) or s.waits_for_environment(self.role):
            return []
        if step.judges_head and s.branch_head and (moved := c.head_moved(s.branch_head)):
            c.escalate(node, "head_moved", moved, resume=Resume.READY)
            return []
        run = self.start(rt.ctx, node)
        return [run] if run else []


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
        node.status = REVIEWING
        if isinstance(after, Verify):
            rt.c.event("review", f"verification passed; panel {list(roles)}", node.id)

    def dispatch(self, rt: StepRunner, node: Node, capacity: int) -> list[Run]:
        """The missing judges' runs at the candidate (one per role and cycle)."""
        c, runs = rt.c, []
        for role in self.roles(c):
            if len(runs) >= capacity:
                break
            busy = any(r.node_id == node.id and r.role == role and r.cycle == node.cycle
                       and r.status in LIVE_RUN | {R_REPORTED} for r in c.runs.values())
            if f"{node.cycle}:{role}" not in node.reviews and not busy \
                    and not rt.scheduler.waits_for_environment(role):
                run = AgentRun(role).start(rt.ctx, node, judged_sha=node.candidate_sha)
                if run:
                    runs.append(run)
        return runs

    def reported(self, rt: StepRunner, node: Node, run: Run, result: PhaseResult | None) -> None:
        """A judge reported: its run is done and its sandbox retired. An escalation stands;
        otherwise the panel decides once every judge has reported – or, where a judge's own
        handler decides (the critic), its result does."""
        run.status = R_ACCEPTED
        rt.lifecycle.sandboxes.delete_later(run.sandbox)
        if self.decides and not isinstance(result, Escalate):
            result = self.decide(node, rt.c)
        super().reported(rt, node, run, result)

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
        """After a producer, its candidate is verified at once; at the head (nothing produced
        it) the verification waits to be dispatched, like any step's first work."""
        if self.at_head:
            node.status = READY
            return
        self._start(rt, node)

    def dispatch(self, rt: StepRunner, node: Node, capacity: int) -> list[Run]:
        if self.at_head and node.status == READY:
            node.candidate_sha = rt.c.change.integration_head
            self._start(rt, node)
        return []

    def _start(self, rt: StepRunner, node: Node) -> None:
        node.status = VERIFYING
        rt.c.spawn(rt.acceptance.verify_node(node.id))

    async def verification(self, ctx: PhaseContext, label: str, sha: str,
                           commands: tuple[str, ...] | None = None) -> Verification:
        """Run it. Never raises. A name the gateway already has (another controller) is
        replaced and the sandbox created again."""
        c = ctx.c
        commands = self.commands_for(None, None) if commands is None else commands
        result = Verification(sha=sha, sandbox=c.host.new_sandbox_name(VERIFIER_PREFIX))
        if not commands and not c.change.setup_pin:
            result.infra_error = "no commands to verify"
            return result
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

    def conclude(self, rt: StepRunner, node: Node, res: Verification, att: str) -> None:
        """Under the lock, once the verification is recorded: the phase's result for the node."""
        result = self.judge(res, node.cycle, att)
        if isinstance(result, Ok):
            for kind, text in self.notes(rt.c, node, res) if self.notes else []:
                rt.c.event(kind, text, node.id)
            rt.advance(node, self)
        elif isinstance(result, Escalate) and result.kind == "setup_failed" \
                and self.on_setup_failed:
            self.on_setup_failed(rt.acceptance, node, setup_failure(res), att, result)
        else:
            rt.ctx.apply(node, result)


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
        node.status = MERGING
        rt.c.spawn(rt.acceptance.merge_node(node.id))

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

    def dispatch(self, rt: StepRunner, node: Node, capacity: int) -> list[Run]:
        if node.status == READY:
            node.status = RUNNING
            rt.c.event("provision", "provisioning the approved environment", node.id)
            rt.c.spawn(rt.provisioner.provision(node.id))
        return []

    def resume(self, rt: StepRunner, node: Node) -> Coroutine[Any, Any, None] | None:
        return rt.provisioner.provision(node.id)

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
        node.status = AWAITING_HUMAN
        rt.c.event("human", "awaiting human decision", node.id)

