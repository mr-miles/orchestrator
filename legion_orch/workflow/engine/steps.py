"""Step types (plan: stages-and-steps.md §2): what each kind of node is made of, declared as a
composition of small objects instead of per-kind code:

* **agent work** – the role of its first :class:`.AgentRun` (the handler in workflow/roles/ is
  the agent layer: prompt, guards, how its report is judged);
* **a sandbox plan** (:mod:`.sandbox_plan`) – where that agent works and how long it is kept;
* **phases** (:mod:`.phases`) – the orchestrator's work, in order, each configured by policies
  (which commands, which judges, what a failure means);
* **waits** – what must hold before it is promoted or dispatched;
* where a failed or blocked run of it resumes.

:class:`StepRunner` is the one place that moves a node through its phases: a phase that passes
hands over to the next, and after the last the node is accepted. Nothing here switches on a
node's kind; the table at the bottom is the only per-kind knowledge.
"""
from __future__ import annotations

from collections.abc import Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..model import (
    ACCEPTED,
    BASELINE,
    BUILD,
    H_ENV,
    H_REQUIREMENTS,
    H_SPEC,
    HUMAN,
    INTAKE,
    INTEGRATION,
    ORACLE,
    PLAN,
    PREFLIGHT,
    PROVISION,
    RELEASE,
    RETRO,
    Node,
    NodeKind,
    Resume,
    Run,
)
from .core import ChangeCore
from .phases import (
    AcceptanceCommands,
    AgentRun,
    AlreadyPassing,
    Approval,
    BaselineCommands,
    BaselineWarnings,
    BuildImages,
    EscalateFailures,
    Judges,
    Merge,
    OracleCommands,
    OracleReworks,
    Panel,
    Phase,
    PhaseContext,
    TaskCommands,
    TierPanel,
    Verify,
)
from .sandbox_plan import JUDGE, Checkout, SandboxPlan, UntilAccepted, UntilGate
from .workflow_tree import in_round

if TYPE_CHECKING:
    from .acceptance import Acceptance
    from .provision import Provisioner
    from .runs import RunLifecycle
    from .schedule import Scheduler


# ---------------------------------------------------------------------------- waits
class Wait:
    """A condition a step waits for (it is not an error while it does not hold)."""

    def holds(self, s: Scheduler, node: Node) -> bool:
        raise NotImplementedError


class NoOverlap(Wait):
    """No running task touches the same files."""

    def holds(self, s: Scheduler, node: Node) -> bool:
        return not s.graph.overlaps_running(node)


class ReflectionsDone(Wait):
    """The last agents' reflections are recorded (they are its material)."""

    def holds(self, s: Scheduler, node: Node) -> bool:
        return not s.lifecycle.sandboxes.reflector.pending


class OracleAccepted(Wait):
    """Its round's oracle is accepted (again, after a preflight sent it back to its author)."""

    def holds(self, s: Scheduler, node: Node) -> bool:
        oracle = s.c.nodes.get(in_round(ORACLE, node.round))
        return oracle is None or oracle.status == ACCEPTED


class EnvironmentQuiescent(Wait):
    """No run or verification uses the current build (it is about to be replaced)."""

    def holds(self, s: Scheduler, node: Node) -> bool:
        return s.environment_quiescent()


# ---------------------------------------------------------------------------- resets
class Reset:
    """What a send-back undoes when it resets a step (the node starting over is generic:
    :meth:`.FlowRuntime._reset_node`). Once per send-back, whichever of its steps it is on."""

    def applies(self, c: ChangeCore, node: Node) -> bool:
        return True

    async def __call__(self, c: ChangeCore, lifecycle: RunLifecycle, node: Node) -> None:
        raise NotImplementedError


class RestartPlan(Reset):
    """The original plan starts over (not a fix round's): everything built on it goes – the
    integration branch back to the base, the oracle, the rounds, the run branches and surface
    pins – and the setup is the base's again."""

    def applies(self, c: ChangeCore, node: Node) -> bool:
        return node.round == 1

    async def __call__(self, c: ChangeCore, lifecycle: RunLifecycle, node: Node) -> None:
        from .. import setup
        from . import cleanup
        ch = c.change
        await c.git.init_integration(ch.id, ch.base_sha)
        ch.integration_head, ch.oracle_sha, ch.oracle = ch.base_sha, None, None
        ch.round, ch.rounds = 1, []
        ch.setup_pin = None if ch.blank_repo else await setup.pin_at_base(c.git, ch.base_sha)
        # a restarted task of the same name must not continue the old plan's commits
        await cleanup.delete_run_branches(c, "plan restarted")
        ch.surface_pins = {}


class DropEnvironment(Reset):
    """The environment is provisioned again from a newly approved spec."""

    async def __call__(self, c: ChangeCore, lifecycle: RunLifecycle, node: Node) -> None:
        c.change.approvals.pop(H_ENV, None)
        c.change.environment = None


# ---------------------------------------------------------------------------- step types
@dataclass(frozen=True)
class StepType:
    phases: tuple[Phase, ...]
    sandbox: SandboxPlan | None = None           # the producing agent's (None: no agent)
    promote_when: tuple[Wait, ...] = ()          # before it starts
    dispatch_when: tuple[Wait, ...] = ()         # before each run is dispatched
    runs_after_end: bool = False                 # dispatched once the change has ended
    rework_resume: Resume = Resume.READY         # where a failed or blocked run resumes
    on_reset: tuple[Reset, ...] = ()             # what a send-back resetting it undoes

    @property
    def role(self) -> str | None:
        first = self.phases[0]
        return first.role if isinstance(first, AgentRun) else None

    @property
    def judges_head(self) -> bool:
        """Its run judges the integration head (G-H1 is checked before it is dispatched)."""
        return bool(self.sandbox and self.sandbox.at_head and not self.runs_after_end)

    def phase(self, cls: type[Phase]) -> Phase | None:
        return next((p for p in self.phases if isinstance(p, cls)), None)

    def judges(self, c: ChangeCore) -> tuple[str, ...]:
        panel = self.phase(Panel)
        return panel.roles(c) if isinstance(panel, Panel) else ()

    def current(self, node: Node) -> Phase | None:
        """The phase the node's status says it is in (None: pending, accepted or escalated)."""
        return next((p for p in self.phases if node.status in p.statuses), None)

    def after(self, phase: Phase) -> Phase | None:
        i = next(i for i, p in enumerate(self.phases) if p is phase)
        return self.phases[i + 1] if i + 1 < len(self.phases) else None


def agent(role: str, sandbox: SandboxPlan, *after: Phase, **kw) -> StepType:
    """A step whose agent works first, then the controller's ``after`` phases."""
    return StepType((AgentRun(role), *after), sandbox=sandbox, **kw)


BASE, HEAD = SandboxPlan(Checkout.BASE), SandboxPlan(Checkout.HEAD)

STEP_TYPES: dict[NodeKind, StepType] = {
    INTAKE: agent("intake", SandboxPlan(Checkout.BASE, UntilGate(H_REQUIREMENTS))),
    PLAN: agent("planner", SandboxPlan(Checkout.BASE, UntilGate(H_ENV)),
                Panel(Judges(("critic",)), decides=False),          # the critic decides (G-C2)
                on_reset=(RestartPlan(),)),
    PROVISION: StepType((BuildImages(),), promote_when=(EnvironmentQuiescent(),),
                        on_reset=(DropEnvironment(),)),
    ORACLE: agent("oracle", SandboxPlan(Checkout.BRANCH, UntilGate(H_SPEC), rebase=True)),  # merges itself
    PREFLIGHT: StepType((Verify(AcceptanceCommands(), expect_failures=True, at_head=True,
                                on_setup_failed=OracleReworks(), notes=AlreadyPassing()),),
                        promote_when=(OracleAccepted(),)),
    BASELINE: agent("baseline", HEAD,
                    Verify(BaselineCommands(), expect_failures=True, notes=BaselineWarnings())),
    BUILD: agent("implementer", SandboxPlan(Checkout.BRANCH, UntilAccepted()),
                 Verify(TaskCommands()), Panel(TierPanel()), Merge(TierPanel()),
                 dispatch_when=(NoOverlap(),), rework_resume=Resume.REWORK),
    INTEGRATION: agent("integration", HEAD,
                       Verify(OracleCommands(), on_rework=EscalateFailures(
                           "integration_failed", "G-V1: {n} oracle command(s) failed at head"))),
    RELEASE: agent("release", HEAD),
    RETRO: agent("retrospective", HEAD, dispatch_when=(ReflectionsDone(),), runs_after_end=True),
    HUMAN: StepType((Approval(),)),
}


def step_for(node: Node) -> StepType:
    return STEP_TYPES[node.kind]


_PRODUCERS = {s.role: s.sandbox for s in STEP_TYPES.values() if s.role and s.sandbox}


def sandbox_plan(role: str) -> SandboxPlan:
    """Where a run of ``role`` works: its step's plan, or – a judge – a fresh sandbox at the
    commit it judges."""
    return _PRODUCERS.get(role, JUDGE)


# ---------------------------------------------------------------------------- the runner
class StepRunner:
    """Moves nodes through their step's phases. Phases call back into it (``advance``) when they
    pass; role handlers do through ``RoleContext.step_done``."""

    def __init__(self, core: ChangeCore, ctx: PhaseContext, lifecycle: RunLifecycle,
                 acceptance: Acceptance, provisioner: Provisioner, scheduler: Scheduler):
        self.c, self.ctx, self.lifecycle = core, ctx, lifecycle
        self.acceptance, self.provisioner, self.scheduler = acceptance, provisioner, scheduler

    @staticmethod
    def step(node: Node) -> StepType:
        return step_for(node)

    def promote(self, node: Node) -> None:
        """Its dependencies are accepted: enter its first phase."""
        self.step(node).phases[0].enter(self, node, None)

    def dispatch(self, node: Node, capacity: int) -> list[Run]:
        """The runs (or controller work) its current phase starts now."""
        phase = self.step(node).current(node)
        return phase.dispatch(self, node, capacity) if phase else []

    def advance(self, node: Node, phase: Phase) -> None:
        """``phase`` passed: enter the next one, or accept the node after the last."""
        nxt = self.step(node).after(phase)
        if nxt is None:
            node.status = ACCEPTED
            self.lifecycle.accept_runs(node)
        else:
            nxt.enter(self, node, phase)

    def step_done(self, node: Node) -> None:
        """The node's current phase passed (an agent's report was accepted)."""
        phase = self.step(node).current(node)
        assert phase is not None, f"{node.id} is not in a phase ({node.status})"
        self.advance(node, phase)

    def resume(self, node: Node) -> Coroutine[Any, Any, None] | None:
        """After a restart: the controller work of a RUNNING node to start again."""
        phase = self.step(node).current(node)
        return phase.resume(self, node) if phase else None
