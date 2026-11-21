"""Step types (plan: stages-and-steps.md §2): one per kind of node, composing its agent work (the
role, and the judges while it is reviewed), the phases the controller runs around it and what
their results mean for it. The scheduler, acceptance and recovery ask the node's step type
instead of switching on its kind.

The agent layer stays in the role handlers (workflow/roles/); the sandbox layer is
:class:`.Sandboxes`; the phases are in :mod:`.phases`."""
from __future__ import annotations

from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from .. import governance
from ..model import (
    ACCEPTED,
    AWAITING_HUMAN,
    BASELINE,
    BUILD,
    HUMAN,
    INTAKE,
    INTEGRATION,
    MERGING,
    ORACLE,
    PLAN,
    PREFLIGHT,
    PROVISION,
    READY,
    RELEASE,
    RETRO,
    REVIEWING,
    RUNNING,
    SHIP,
    VERIFYING,
    CommandResult,
    Node,
    NodeKind,
    Oracle,
    Resume,
    Run,
    Verification,
)
from ..roles import handler_for
from .common import _slug
from .core import ChangeCore
from .phases import AgentRun, Escalate, Ok, PhaseResult, Rework, Verify, setup_failure

if TYPE_CHECKING:
    from .acceptance import Acceptance
    from .schedule import Scheduler


class StepType:
    """The defaults: no agent, no verification, ready once its dependencies are accepted."""
    role: str | None = None              # the agent that does the step's work
    runs_after_end = False               # dispatched once the change has ended
    rework_resume = Resume.READY         # where a failed or blocked run of it resumes

    # ------------------------------------------------------------------ scheduling
    def judges(self, c: ChangeCore) -> tuple[str, ...]:
        """The judges dispatched at the candidate while the node is REVIEWING."""
        return ()

    def may_promote(self, s: Scheduler) -> bool:
        """Its dependencies are accepted: may it start now?"""
        return True

    def promote(self, s: Scheduler, node: Node) -> None:
        node.status = READY

    def start(self, s: Scheduler, node: Node) -> list[Run]:
        """The node is READY (or in REWORK): the runs to launch (outside the lock)."""
        return []

    @property
    def judges_head(self) -> bool:
        """Its run judges the integration head (G-H1 is checked before it is dispatched)."""
        return False

    def controller_work(self, s: Scheduler) -> Coroutine[Any, Any, None] | None:
        """The controller's own work while the node is RUNNING (resumed after a restart)."""
        return None

    # ------------------------------------------------------------------ verification
    def verify(self, c: ChangeCore, node: Node) -> Verify:
        raise NotImplementedError(f"{node.kind} is not verified")

    def verification_record(self, node: Node) -> tuple[str, str]:
        """Where the verification is stored, and its key on the node."""
        return f"control/verification/{_slug(node.id)}.json", str(node.kind)

    def verified(self, a: Acceptance, node: Node, res: Verification, result: PhaseResult,
                 att: str) -> None:
        """Under the lock: what the (recorded) verification means for the node. A rework or an
        escalation is applied; ``on_verified`` says what passing means."""
        if isinstance(result, Ok):
            self.on_verified(a, node, res)
        else:
            a.ctx.apply(node, result)

    def on_verified(self, a: Acceptance, node: Node, res: Verification) -> None:
        node.status = ACCEPTED
        a.lifecycle.accept_runs(node)


# ---------------------------------------------------------------------------- agent steps
class AgentStep(StepType):
    """A role's agent does the work; its handler judges the report."""

    def __init__(self, role: str):
        self.role = role

    @property
    def judges_head(self) -> bool:
        return handler_for(self.role).judges_head and not self.runs_after_end

    def may_dispatch(self, s: Scheduler, node: Node) -> bool:
        return True

    def start(self, s: Scheduler, node: Node) -> list[Run]:
        c = s.c
        if not s.graph.deps_accepted(node) or not self.may_dispatch(s, node):
            return []
        if s.waits_for_environment(self.role):
            return []
        if s.judges_head(node) and s.branch_head and (moved := c.head_moved(s.branch_head)):
            c.escalate(node, "head_moved", moved, resume=Resume.READY)
            return []
        run = AgentRun(self.role).start(s.acceptance.ctx, node)
        return [run] if run else []


class Plan(AgentStep):
    """The planner, judged by the critic (its REWORK loops back within G-C2's budget)."""

    def judges(self, c: ChangeCore) -> tuple[str, ...]:
        return ("critic",)


class BuildTask(AgentStep):
    """AgentRun → Verify(task) → Panel(tier) → Merge (the scaffold too)."""
    rework_resume = Resume.REWORK

    def judges(self, c: ChangeCore) -> tuple[str, ...]:
        return tuple(c.change.panel)

    def may_dispatch(self, s: Scheduler, node: Node) -> bool:
        return not s.graph.overlaps_running(node)

    def verify(self, c: ChangeCore, node: Node) -> Verify:
        return Verify(tuple(node.task.verification))

    def verification_record(self, node: Node) -> tuple[str, str]:
        return f"tasks/{node.task.id}/verification.json", str(node.cycle)

    def on_verified(self, a: Acceptance, node: Node, res: Verification) -> None:
        c = a.c
        if c.change.panel:
            node.status = REVIEWING
            c.event("review", f"verification passed; panel {c.change.panel}", node.id)
        else:
            node.status = MERGING
            c.spawn(a.merge_node(node.id))


class Baseline(AgentStep):
    """AgentRun → Verify(oracle, expect_failures): before any build, failures are expected:
    they are recorded, not reworked (G-BL2 flags at H-2 what already passes or differs from the
    agent's results)."""

    def verify(self, c: ChangeCore, node: Node) -> Verify:
        return Verify(tuple(c.baseline_commands()), expect_failures=True)

    def on_verified(self, a: Acceptance, node: Node, res: Verification) -> None:
        super().on_verified(a, node, res)
        for w in governance.baseline_warnings(a.c):
            a.c.event("baseline", w, node.id)


class Integration(AgentStep):
    """AgentRun → Verify(oracle) at the head. Nothing reworks here: a failure escalates."""

    def verify(self, c: ChangeCore, node: Node) -> Verify:
        return Verify(tuple(c.oracle_commands()))

    def verified(self, a: Acceptance, node: Node, res: Verification, result: PhaseResult,
                 att: str) -> None:
        if isinstance(result, Rework):
            fails = [r for r in res.results if r.exit_code != 0]
            result = Escalate("integration_failed",
                              f"G-V1: {len(fails)} oracle command(s) failed at head", Resume.READY)
        super().verified(a, node, res, result, att)


class Retrospective(AgentStep):
    """Read the ended change – after its agents' last reflections."""
    runs_after_end = True

    def may_dispatch(self, s: Scheduler, node: Node) -> bool:
        return not s.lifecycle.sandboxes.reflector.pending


# ---------------------------------------------------------------------------- controller steps
class HumanGate(StepType):
    def promote(self, s: Scheduler, node: Node) -> None:
        node.status = AWAITING_HUMAN
        s.c.event("human", "awaiting human decision", node.id)


class Provision(StepType):
    """BuildImages → Verify(setup) (G-PV3), once no run or verification uses the build."""

    def may_promote(self, s: Scheduler) -> bool:
        return s.environment_quiescent()

    def start(self, s: Scheduler, node: Node) -> list[Run]:
        if node.status == READY:
            node.status = RUNNING
            s.c.event("provision", "provisioning the approved environment", node.id)
            s.c.spawn(s.provisioner.provision())
        return []

    def controller_work(self, s: Scheduler) -> Coroutine[Any, Any, None] | None:
        return s.provisioner.provision()


class Preflight(StepType):
    """Verify(acceptance commands, expect_failures) at the integration head: the oracle's
    commands can be run there (G-PF1). A failed setup goes back to the oracle's author."""

    def promote(self, s: Scheduler, node: Node) -> None:
        node.candidate_sha = s.c.change.integration_head
        node.status = VERIFYING
        s.c.spawn(s.acceptance.verify_node(node.id))

    def verify(self, c: ChangeCore, node: Node) -> Verify:
        return Verify(tuple((c.change.oracle or Oracle()).acceptance_commands),
                      expect_failures=True)

    def verified(self, a: Acceptance, node: Node, res: Verification, result: PhaseResult,
                 att: str) -> None:
        if isinstance(result, Escalate) and result.kind == "setup_failed":
            setup: CommandResult = setup_failure(res)
            a.setup_failed(node, setup, att, result)
        else:
            super().verified(a, node, res, result, att)

    def on_verified(self, a: Acceptance, node: Node, res: Verification) -> None:
        # before the build the commands are expected to fail, so one that already passes is
        # only noted
        node.status = ACCEPTED
        for r in res.results:
            if not r.setup and r.exit_code == 0:
                a.c.event("preflight", f"acceptance command `{r.command}` already passes "
                          "before any build: it may not test the change", node.id)


class Ship(StepType):
    """Evaluate(ship gates): the readiness verdict is the change's status."""

    def promote(self, s: Scheduler, node: Node) -> None:
        node.status = RUNNING
        s.c.spawn(s.acceptance.ship())

    def controller_work(self, s: Scheduler) -> Coroutine[Any, Any, None] | None:
        return s.acceptance.ship()


STEP_TYPES: dict[NodeKind, StepType] = {
    INTAKE: AgentStep("intake"),
    PLAN: Plan("planner"),
    ORACLE: AgentStep("oracle"),
    BASELINE: Baseline("baseline"),
    BUILD: BuildTask("implementer"),
    INTEGRATION: Integration("integration"),
    RELEASE: AgentStep("release"),
    RETRO: Retrospective("retrospective"),
    HUMAN: HumanGate(),
    PROVISION: Provision(),
    PREFLIGHT: Preflight(),
    SHIP: Ship(),
}


def step_for(node: Node) -> StepType:
    return STEP_TYPES[node.kind]
