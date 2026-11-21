"""A change's workflow as stages and steps (plan: stages-and-steps.md §3), built for its tier,
environment and repository:

```
Requirements   intake ───────────────────────────────────────────────── ◆ H-1
Plan                                                                  ◆ plan-approved (automatic)
  Design       plan (planner ⇄ critic) ──────────────────────────────── ◆ H-E1 "approve the plan"
  Oracle       [provision] [scaffold] [oracle preflight] [baseline] ──── ◆ H-2 "approve the oracle"
Delivery       tasks (DAG) ─ [integration] ─ [release] ───────────────── ◆ H-3 "accept the change"
```

Step ids are the engine's node ids; a fix round's are suffixed ``@n`` (``plan@2``, ``H-2@2``).
The build tasks are known once the plan is accepted: :func:`task_steps` gives the steps of the
``tasks`` group (and a blank repository's ``scaffold`` group, before the oracle).
"""
from __future__ import annotations

from ..flow import AppendRound, Gate, Parallel, Stage, StartOver, Step, Workflow
from ..model import (
    BASELINE,
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
    RELEASE,
    NodeKind,
)

TASKS, SCAFFOLD = "tasks", "scaffold"          # the parallel groups of build steps

# the engine node each step or gate of the tree is (build steps come with their task)
NODES: dict[str, tuple[NodeKind, str]] = {
    INTAKE: (NodeKind.INTAKE, "Intake: requirements & risk"),
    H_REQUIREMENTS: (HUMAN, "Accept requirements & tier"),
    PLAN: (NodeKind.PLAN, "Plan: spec, oracle, task graph; critique"),
    H_ENV: (HUMAN, "Approve the plan"),
    PROVISION: (NodeKind.PROVISION, "Provision: environment images"),
    ORACLE: (NodeKind.ORACLE, "Oracle: protected acceptance tests"),
    PREFLIGHT: (NodeKind.PREFLIGHT, "Preflight: the oracle's commands run"),
    BASELINE: (NodeKind.BASELINE, "Independent baseline"),
    H_SPEC: (HUMAN, "Approve the oracle"),
    INTEGRATION: (NodeKind.INTEGRATION, "Integration verification"),
    RELEASE: (NodeKind.RELEASE, "Release & rollback plan"),
    H_CHANGE: (HUMAN, "Accept the change"),
}
PLAN_GATE = "plan-approved"


def stage_id(name: str, n: int = 1) -> str:
    return f"stage:{name}" if n == 1 else f"stage:{name}@{n}"


def in_round(nid: str, n: int) -> str:
    """A node's id in fix round ``n`` (round 1 is the original work)."""
    return nid if n == 1 else f"{nid}@{n}"


def round_of(nid: str) -> int:
    base, _, n = nid.rpartition("@")
    return int(n) if base and n.isdigit() else 1


def base_id(nid: str) -> str:
    """The id without its round suffix."""
    return nid.rpartition("@")[0] if round_of(nid) > 1 else nid


def _design(n: int) -> Stage:
    return Stage(stage_id("design", n), [Step(in_round(PLAN, n))], Gate(in_round(H_ENV, n)))


def _oracle(rank: int, environment: bool, blank_repo: bool, n: int) -> Stage | None:
    """Everything that makes the oracle and the sandboxes ready, before any task is built."""
    children: list[Stage | Step | Parallel] = []
    if environment:
        children.append(Step(in_round(PROVISION, n)))
    if blank_repo and n == 1:                     # the scaffold is built once (§5.3b)
        children.append(Parallel(SCAFFOLD, []))
    if rank >= 2:
        children += [Step(in_round(ORACLE, n)), Step(in_round(PREFLIGHT, n))]
    if rank >= 3:
        children.append(Step(in_round(BASELINE, n)))
    if not children:
        return None
    # R3: the independent baseline's attestation is a precondition of approving the oracle
    gate = Gate(in_round(H_SPEC, n), preconditions=rank >= 3, automatic=rank < 2)
    return Stage(stage_id("oracle", n), children, gate)


def round_template(rank: int, environment: bool):
    """Fix round ``n`` (§5.10a): plan new tasks on top of what is merged, re-make what they
    change (environment, oracle – their steps pass unchanged when nothing changed), build them."""
    def template(n: int) -> Stage:
        children: list[Stage | Step | Parallel] = [_design(n)]
        if oracle := _oracle(rank, environment, False, n):
            children.append(oracle)
        children.append(Parallel(in_round(TASKS, n), []))
        return Stage(stage_id("round", n), children, Gate(f"round-{n}-approved", automatic=True))
    return template


def requirements_workflow() -> Workflow:
    """Before H-1: only the requirements are known (the tier decides the rest)."""
    return Workflow([Stage(stage_id("requirements"), [Step(INTAKE)], Gate(H_REQUIREMENTS))])


def build_workflow(rank: int, *, environment: bool, blank_repo: bool) -> Workflow:
    """The change's tree. ``rank``: the tier's (risk.rank); stages without content are left out,
    and gates without a human decision at the tier are automatic."""
    requirements = requirements_workflow().stages[0]
    plan_children: list[Stage | Step | Parallel] = [_design(1)]
    if oracle := _oracle(rank, environment, blank_repo, 1):
        plan_children.append(oracle)
    plan = Stage(stage_id("plan"), plan_children, Gate(PLAN_GATE, automatic=True),
                 rework=StartOver(), feedback_to=PLAN)
    delivery_children: list[Stage | Step | Parallel] = [Parallel(TASKS, [])]
    if rank >= 2:
        delivery_children.append(Step(INTEGRATION))
    if rank >= 3:
        delivery_children.append(Step(RELEASE))
    delivery = Stage(stage_id("delivery"), delivery_children,
                     # the ship gates are its preconditions; nobody accepts R0/R1 by hand
                     Gate(H_CHANGE, preconditions=True, automatic=rank < 2, rejectable=True),
                     rework=AppendRound(round_template(rank, environment),
                                        before=INTEGRATION if rank >= 2 else None))
    return Workflow([requirements, plan, delivery])


def task_steps(taskgraph: dict, *, built: set[str] = frozenset()) -> tuple[list[Step], list[Step]]:
    """The scaffold's and the other tasks' steps of an accepted task graph (dependencies within
    the group; the scaffold comes before every task anyway). ``built``: task ids of earlier
    rounds, which stay as they are."""
    tasks = [t for t in taskgraph["tasks"] if t["id"] not in built]
    scaffold = [t for t in tasks if t.get("scaffold")]
    rest = [t for t in tasks if not t.get("scaffold")]
    ids = {t["id"] for t in rest}

    def step(t: dict) -> Step:
        return Step(f"build:{t['id']}", overridable=True,
                    depends_on=[f"build:{d}" for d in t.get("depends_on", []) if d in ids])
    return [step(t) for t in scaffold], [step(t) for t in rest]
