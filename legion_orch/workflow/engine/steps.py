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
    BASELINE,
    BUILD,
    H_ENV,
    H_REQUIREMENTS,
    H_SPEC,
    HUMAN,
    INTAKE,
    INTEGRATION,
    ORACLE,
    PENDING,
    PLAN,
    PREFLIGHT,
    PROVISION,
    R_REPORTED,
    R_SUPERSEDED,
    READY,
    RELEASE,
    RETRO,
    REWORK,
    FailureSource,
    Feedback,
    Node,
    NodeKind,
    NodeStatus,
    Resume,
    Run,
)
from ..results import Escalate, Redo, Rework
from .core import ChangeCore
from .node_states import NodeStates
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
    PhaseResult,
    TaskCommands,
    TierPanel,
    Verify,
)
from .sandbox_plan import JUDGE, Checkout, SandboxPlan, UntilAccepted, UntilGate

if TYPE_CHECKING:
    from .acceptance import Acceptance
    from .provision import Provisioner
    from .runs import RunLifecycle
    from .runtime import FlowRuntime
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


class EnvironmentQuiescent(Wait):
    """No run or verification uses the current build (it is about to be replaced)."""

    def holds(self, s: Scheduler, node: Node) -> bool:
        return s.environment_quiescent()


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
    dispatch_when: tuple[Wait, ...] = ()         # before its work starts (each run, each try)
    runs_after_end: bool = False                 # dispatched once the change has ended
    rework_resume: Resume = Resume.READY         # where a failed or blocked run resumes
    stands: Stands | None = None                 # a round's step with nothing to redo passes
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
    """The step layer: moves nodes through their step's phases and owns their states. Above it
    the workflow runtime starts, stops, resets and resumes steps; below it the phases enter,
    dispatch and report their results. At the boundaries of a step's states – done, escalated,
    resumed, an earlier step redone – it tells the runtime, and nothing else does."""

    def __init__(self, core: ChangeCore, ctx: PhaseContext, lifecycle: RunLifecycle,
                 acceptance: Acceptance, provisioner: Provisioner, scheduler: Scheduler):
        self.c, self.ctx, self.lifecycle = core, ctx, lifecycle
        self.acceptance, self.provisioner, self.scheduler = acceptance, provisioner, scheduler
        self.states = NodeStates()
        self.runtime: FlowRuntime = None  # type: ignore[assignment]   # set once it exists

    @staticmethod
    def step(node: Node) -> StepType:
        return step_for(node)

    # ------------------------------------------------------------------ from the runtime (down)
    def start(self, node: Node, feedback: list[Feedback]) -> None:
        """The core started the node's step: a new cycle if it ran before, with the feedback
        it was sent back with. An agent that ran continues in its kept session (REWORK); a
        round's step that has nothing to redo stands; anything else enters its first phase."""
        if node.runs or node.status != PENDING:
            node.cycle += 1
            node.candidate_sha = node.accepted_sha = None
        for f in feedback:
            node.feedback.append(f.model_copy(update={"cycle": node.cycle}))
        step = self.step(node)
        if node.runs and step.role:
            self.states.reset(node)
            self.states.phase(node, REWORK)
            return
        self.states.reset(node)
        if step.stands and step.stands.holds(self.c, node):
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
        self.states.reset(node)

    def resume(self, node: Node, to: NodeStatus) -> None:
        """The node's escalation is resolved: its step goes on in the phase ``to``."""
        self.states.resume(node, to)
        self.runtime.resumed(node)

    def end(self, node: Node) -> None:
        """The change ended at this node (abandoned at its escalation)."""
        self.states.end(node)

    def again(self, node: Node) -> None:
        """A node outside the workflow's tree (the retrospective) runs again."""
        self.states.again(node)

    def resume_work(self, node: Node) -> Coroutine[Any, Any, None] | None:
        """After a restart: the controller work of a RUNNING node to start again."""
        phase = self.step(node).current(node)
        return phase.resume(self, node) if phase else None

    # ------------------------------------------------------------------ from the clock
    def dispatch(self, node: Node, capacity: int) -> list[Run]:
        """The runs (or controller work) its current phase may start now: a step's first
        work waits until its step type's ``dispatch_when`` hold."""
        step = self.step(node)
        phase = step.current(node)
        if phase is None:
            return []
        if phase is step.phases[0] and node.status in (READY, REWORK) \
                and not all(w.holds(self.scheduler, node) for w in step.dispatch_when):
            return []
        return phase.dispatch(self, node, capacity)

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

    def reported(self, node: Node, run: Run, result: PhaseResult | None) -> None:
        """An agent's turn ended after an accepted report: its role handler's result goes to
        the node's current phase, which applies it."""
        phase = self.step(node).current(node)
        assert phase is not None, f"{node.id} is not in a phase ({node.status})"
        phase.reported(self, node, run, result)

    def run_failed(self, node: Node, run: Run, result: Escalate) -> None:
        """A run ended in failure (its service reports it): the phase the node is in decides
        (a human, always: no automatic retries). A node no longer in a phase just keeps the
        record."""
        phase = self.step(node).current(node)
        if phase is not None:
            phase.failed(self, node, run, result)

    def apply(self, node: Node, result: PhaseResult) -> None:
        """A phase's result that does not pass it: rework within the budget, an escalation, or
        an earlier step redone."""
        if isinstance(result, Rework):
            self.rework(node, result.feedback, result.source, result.budget_checked)
            if result.base and node.status == REWORK:
                node.base_sha = result.base
        elif isinstance(result, Escalate):
            node.feedback += result.feedback
            self.escalate(node, result.kind, result.question, result.resume, run=result.run,
                          missing_decision=result.missing_decision, log_tail=result.log_tail,
                          source=result.source)
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

    # ------------------------------------------------------------------ the boundaries
    def accept(self, node: Node) -> None:
        """The step's work is done: its runs are accepted, and the workflow is told."""
        self.states.accept(node)
        self.lifecycle.accept_runs(node)
        self.runtime.completed(node)

    def escalate(self, node: Node, kind: str, reason: str, resume: Resume, *,
                 run: str | None = None, missing_decision: str | None = None,
                 log_tail: str | None = None, source: FailureSource | None = None) -> None:
        """A human decides; ``resume`` says where the step continues once they have."""
        self.states.escalate(node, kind, reason, resume, run=run,
                             missing_decision=missing_decision, log_tail=log_tail, source=source)
        self.c.event("escalation", f"{kind}: {reason}", node.id, run)
        self.runtime.escalated(node)
