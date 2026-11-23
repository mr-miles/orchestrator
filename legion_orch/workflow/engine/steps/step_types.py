"""Step types (plan: stages-and-steps.md §2): what each kind of node is made of, declared as a
composition of small objects instead of per-kind code:

* **agent work** – the role of its producer, :class:`.Produce` (the handler in workflow/roles/ is
  the agent layer: prompt, guards, how its report is judged);
* **a sandbox plan** (:mod:`.sandbox_plan`) – where that agent works and how long it is kept;
* **phases** (:mod:`.phases`) – the orchestrator's work, in order, each configured by policies
  (which commands, which judges, what a failure means);
* **waits** – what must hold before it is promoted or dispatched;
* where a failed or blocked run of it resumes.

:class:`.ActiveStep` (:mod:`.active`) is the one place that moves a node through its phases: a
phase that passes hands over to the next, and after the last the node is accepted. Nothing here switches on a
node's kind; the table at the bottom is the only per-kind knowledge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....git import paths_overlap
from ....util import now_iso
from ...model import (
    ACTIVE_NODE,
    BASELINE,
    BUILD,
    DESCRIBE,
    H_CHANGE,
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
    PUBLISH,
    RELEASE,
    RETRO,
    REWORK,
    WORKSPACE,
    Node,
    NodeKind,
    NodeStatus,
    Resume,
)
from ..base.core import ChangeCore
from ..base.ids import base_id
from ..sandboxes.plan import JUDGE, Checkout, SandboxPlan, UntilAccepted, UntilGate
from .approvals import ChangeAccepted, RequirementsAccepted, SubjectsApproved
from .phases import (
    AcceptanceCommands,
    AlreadyPassing,
    Approval,
    AutomaticBelow,
    BaselineCommands,
    BaselineWarnings,
    BuildImages,
    Critique,
    DefineTasks,
    EscalateFailures,
    Merge,
    OpenPullRequest,
    OracleCommands,
    OracleReworks,
    Panel,
    Phase,
    Produce,
    RiskLabel,
    ShipGates,
    SyncWorkspace,
    TaskCommands,
    TierPanel,
    Verify,
    _judges,
)

if TYPE_CHECKING:
    from ..services.runs import RunLifecycle
    from .active import ActiveStep


# ---------------------------------------------------------------------------- waits
class Wait:
    """A condition a step waits for (it is not an error while it does not hold)."""

    def holds(self, step: ActiveStep) -> bool:
        raise NotImplementedError


class NoOverlap(Wait):
    """No running task touches the same files."""

    def holds(self, step: ActiveStep) -> bool:
        """Guard G-D3 (its file-overlap part)."""
        node = step.node
        mine = node.task.files_modified
        for other in step.c.build_nodes():
            if other.id == node.id or other.status not in ACTIVE_NODE | {REWORK}:
                continue
            if any(paths_overlap(a, b) for a in mine for b in other.task.files_modified):
                return False
        return True


class ReflectionsDone(Wait):
    """The last agents' reflections are recorded (they are its material)."""

    def holds(self, step: ActiveStep) -> bool:
        return not step.lifecycle.sandboxes.reflector.pending


class EnvironmentQuiescent(Wait):
    """No run or verification uses the current build (it is about to be replaced)."""

    def holds(self, step: ActiveStep) -> bool:
        return step.provisioner.environment_quiescent()


# ---------------------------------------------------------------------------- standing
class Stands:
    """When a fix round's step has nothing to redo: it passes at once, its work standing."""
    kind = "step"
    why = ""

    def holds(self, c: ChangeCore, node: Node) -> bool:
        raise NotImplementedError

    def keep(self, c: ChangeCore, node: Node) -> None:
        """What the step's node keeps of the work that stands."""


class OracleUnchanged(Stands):
    """A round that left ``plan/oracle.json`` as approved keeps the oracle commit."""
    kind, why = "oracle", "plan/oracle.json is unchanged: the oracle stands"

    def holds(self, c: ChangeCore, node: Node) -> bool:
        orc = c.change.approvals.get("oracle")
        return node.round > 1 and orc is not None and \
            orc.pins.get(c.p("plan/oracle.json")) == c.latest("plan/oracle.json")

    def keep(self, c: ChangeCore, node: Node) -> None:
        node.accepted_sha = c.change.oracle_sha


class EnvironmentUnchanged(Stands):
    """A round that left the environment spec as approved keeps the build."""
    kind, why = "provision", "the environment spec is unchanged: the build stands"

    def holds(self, c: ChangeCore, node: Node) -> bool:
        env = c.change.approvals.get(H_ENV)
        return node.round > 1 and c.change.environment is not None and env is not None and \
            env.pins.get(c.p("plan/environment.json")) == c.latest("plan/environment.json")


# ---------------------------------------------------------------------------- resets
class Reset:
    """What a send-back undoes when it resets a step (the node starting over is generic:
    the step runner's). Once per send-back, whichever of its steps it is on."""

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
        from ... import setup
        from ..services import cleanup
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
        if H_ENV in c.change.approvals:            # revoked: its document says so (projected)
            c.write_record("control/approvals/environment.json",
                           {"gate": H_ENV, "approver": "controller", "at": now_iso(),
                            "revoked": True})
        c.change.environment = None


# ---------------------------------------------------------------------------- step types
@dataclass(frozen=True)
class StepType:
    phases: tuple[Phase, ...]
    sandbox: SandboxPlan | None = None           # the producing agent's (None: no agent)
    dispatch_when: tuple[Wait, ...] = ()         # before its work starts (each run, each try)
    runs_after_end: bool = False                 # dispatched once the change has ended
    rework_resume: Resume = Resume.READY         # where a failed or blocked run resumes
    stands: Stands | None = None                 # a round's step with nothing to redo passes
    on_reset: tuple[Reset, ...] = ()             # what a send-back resetting it undoes

    @property
    def role(self) -> str | None:
        first = self.phases[0]
        return first.role if isinstance(first, Produce) else None

    def agent_roles(self, c) -> list[str]:
        """The roles of its agents – producer, critic, the change's panel – in phase order: each
        has an A2A card while the step is in progress (workflow/cards.py)."""
        roles: list[str] = []
        for p in self.phases:
            if isinstance(p, (Produce, Critique)):
                roles.append(p.role)
            elif isinstance(p, Panel):
                roles += _judges(p.judges, c)
        return list(dict.fromkeys(roles))

    @property
    def judges_head(self) -> bool:
        """Its run judges the integration head (G-H1 is checked before it is dispatched)."""
        return bool(self.sandbox and self.sandbox.at_head and not self.runs_after_end)

    def phase(self, cls: type[Phase]) -> Phase | None:
        return next((p for p in self.phases if isinstance(p, cls)), None)

    def phase_in(self, status: NodeStatus) -> Phase | None:
        """The phase a node is in while it has ``status`` (None: pending, accepted, waiting)."""
        return next((p for p in self.phases if status in p.statuses), None)

    def after(self, phase: Phase) -> Phase | None:
        """The phase that follows ``phase`` (None: it is the last)."""
        i = next(i for i, p in enumerate(self.phases) if p is phase)
        return self.phases[i + 1] if i + 1 < len(self.phases) else None


def agent(role: str, sandbox: SandboxPlan, *after: Phase, **kw) -> StepType:
    """A step whose agent works first, then the controller's ``after`` phases."""
    return StepType((Produce(role), *after), sandbox=sandbox, **kw)


BASE, HEAD = SandboxPlan(Checkout.BASE), SandboxPlan(Checkout.HEAD)

STEP_TYPES: dict[NodeKind, StepType] = {
    WORKSPACE: StepType((SyncWorkspace(),)),       # the controller's: no agent, no sandbox
    INTAKE: agent("intake", SandboxPlan(Checkout.BASE, UntilGate(H_REQUIREMENTS))),
    PLAN: agent("planner", SandboxPlan(Checkout.BASE, UntilGate(H_ENV)),
                Critique("critic", "G-C2"), DefineTasks(),                         # the critic judges (G-C2)
                on_reset=(RestartPlan(),)),
    PROVISION: StepType((BuildImages(),), dispatch_when=(EnvironmentQuiescent(),),
                        stands=EnvironmentUnchanged(),
                        on_reset=(DropEnvironment(),)),
    ORACLE: agent("oracle", SandboxPlan(Checkout.BRANCH, UntilGate(H_SPEC), rebase=True),
                  stands=OracleUnchanged()),                     # merges itself
    PREFLIGHT: StepType((Verify(AcceptanceCommands(), expect_failures=True, at_head=True,
                                on_setup_failed=OracleReworks(), notes=AlreadyPassing()),)),
    BASELINE: agent("baseline", HEAD,
                    Verify(BaselineCommands(), expect_failures=True, notes=BaselineWarnings())),
    BUILD: agent("implementer", SandboxPlan(Checkout.BRANCH, UntilAccepted()),
                 Verify(TaskCommands()), Panel(TierPanel()), Merge(),
                 dispatch_when=(NoOverlap(),), rework_resume=Resume.REWORK),
    INTEGRATION: agent("integration", HEAD,
                       Verify(OracleCommands(), on_rework=EscalateFailures(
                           "integration_failed", "G-V1: {n} oracle command(s) failed at head"))),
    DESCRIBE: agent("describer", HEAD, Critique("pr-reviewer", "G-PR3")),
    PUBLISH: StepType((OpenPullRequest((RiskLabel(),)),)),   # after H-3: the controller's
    RELEASE: agent("release", HEAD),               # in no tier's tree (live-system workflows)
    RETRO: agent("retrospective", HEAD, dispatch_when=(ReflectionsDone(),), runs_after_end=True),
}

#: each gate's step: what approves it, what it checks, when it passes by itself
GATE_TYPES: dict[str, StepType] = {
    H_REQUIREMENTS: StepType((Approval(RequirementsAccepted()),)),     # H-1: the tier with it
    H_ENV: StepType((Approval(SubjectsApproved("plan")),)),
    H_SPEC: StepType((Approval(SubjectsApproved("oracle", baseline_from=3)),)),
    H_CHANGE: StepType((Approval(ChangeAccepted(), checks=ShipGates(),
                                 automatic=AutomaticBelow(2)),)),
}


def step_for(node: Node) -> StepType:
    if node.kind == HUMAN:
        return GATE_TYPES[base_id(node.id)]
    return STEP_TYPES[node.kind]


_PRODUCERS = {s.role: s.sandbox for s in STEP_TYPES.values() if s.role and s.sandbox}
# the change's resolver is no step's: one sandbox for the change, at the base (its consults)
_PRODUCERS["resolver"] = BASE


def sandbox_plan(role: str) -> SandboxPlan:
    """Where a run of ``role`` works: its step's plan, or – a judge – a fresh sandbox at the
    commit it judges."""
    return _PRODUCERS.get(role, JUDGE)


class StepHolds:
    """Whether a node still holds its kept sandbox, as its step type's sandbox plan says – for
    the sandbox layer (:class:`.Holds`)."""

    def __init__(self, core: ChangeCore):
        self.c = core

    def holds(self, node: Node) -> bool:
        plan = step_for(node).sandbox
        return plan is not None and plan.held(self.c, node)
