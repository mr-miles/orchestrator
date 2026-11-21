"""A change's workflow as stages and steps (plan: stages-and-steps.md §3), built for its tier,
environment and repository:

```
Requirements   workspace ─ intake ───────────────────────────────────── ◆ H-1
Plan           (done when its stages are)
  Design       plan (planner ⇄ critic) ──────────────────────────────── ◆ H-E1 "approve the plan"
  Oracle       [provision] [scaffold] [oracle preflight] [baseline] ──── ◆ H-2 "approve the oracle"
Delivery       tasks (DAG) ─ [integration] ─ describe ───────────────── ◆ H-3 "accept the change"
[Publish]      publish (the source is on GitHub: the pull request, opened)
```

A gate (◆) is a step that ends its stage: its work checks what it needs and waits for a human's
approval (engine/steps.py). Step ids are the engine's node ids; a fix round's are suffixed
``@n`` (``plan@2``, ``H-2@2``). The build tasks are known once the plan is accepted:
:func:`task_steps` gives the steps of the ``tasks`` group (and a blank repository's ``scaffold``
group, before the oracle)."""
from __future__ import annotations

from ...flow import AppendRound, Parallel, Stage, StartOver, Step, Workflow
from ...model import (
    BASELINE,
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
    WORKSPACE,
    NodeKind,
)
from ..base.ids import base_id, in_round, round_of, stage_id, stage_title  # noqa: F401  (re-exported)

TASKS, SCAFFOLD = "tasks", "scaffold"          # the parallel groups of build steps

# the engine node each step or gate of the tree is (build steps come with their task)
NODES: dict[str, tuple[NodeKind, str]] = {
    WORKSPACE: (NodeKind.WORKSPACE, "Workspace: synced, the change's base"),
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
    DESCRIBE: (NodeKind.DESCRIBE, "Describe: the pull request, reviewed"),
    PUBLISH: (NodeKind.PUBLISH, "Publish: the pull request opened"),
    H_CHANGE: (HUMAN, "Accept the change"),
}


def _design(n: int) -> Stage:
    return Stage(stage_id("design", n), [Step(in_round(PLAN, n))], Step(in_round(H_ENV, n)))


def _oracle(rank: int, environment: bool, blank_repo: bool, n: int) -> Stage | None:
    """Everything that makes the oracle and the sandboxes ready, before any task is built."""
    children: list[Stage | Step | Parallel] = []
    if environment:
        children.append(Step(in_round(PROVISION, n), rerunnable=True))   # an answer: build again
    if blank_repo and n == 1:                     # the scaffold is built once (§5.3b)
        children.append(Parallel(SCAFFOLD, []))
    if rank >= 2:
        children += [Step(in_round(ORACLE, n)), Step(in_round(PREFLIGHT, n))]
    if rank >= 3:
        children.append(Step(in_round(BASELINE, n)))
    if not children:
        return None
    # below R2 nobody approves the oracle (its gate step's policies: engine/steps.py)
    return Stage(stage_id("oracle", n), children, Step(in_round(H_SPEC, n)) if rank >= 2 else None)


def round_template(rank: int, environment: bool):
    """Fix round ``n`` (§5.10a): plan new tasks on top of what is merged, re-make what they
    change (environment, oracle – their steps pass unchanged when nothing changed), build them."""
    def template(n: int) -> Stage:
        children: list[Stage | Step | Parallel] = [_design(n)]
        if oracle := _oracle(rank, environment, False, n):
            children.append(oracle)
        children.append(Parallel(in_round(TASKS, n), []))
        return Stage(stage_id("round", n), children)           # approved with its gates
    return template


def requirements_workflow() -> Workflow:
    """Before H-1: only the requirements are known (the tier decides the rest)."""
    return Workflow([Stage(stage_id("requirements"), [Step(WORKSPACE), Step(INTAKE)],
                           Step(H_REQUIREMENTS), feedback_to=INTAKE)])


def build_workflow(rank: int, *, environment: bool, blank_repo: bool, publish: bool) -> Workflow:
    """The change's tree. ``rank``: the tier's (risk.rank); stages without content are left out,
    and gates without a human decision at the tier left out (H-3 stays: its checks, the ship
    gates, hold at every tier – below R2 it passes by itself once they do)."""
    requirements = requirements_workflow().stages[0]
    plan_children: list[Stage | Step | Parallel] = [_design(1)]
    if oracle := _oracle(rank, environment, blank_repo, 1):
        plan_children.append(oracle)
    plan = Stage(stage_id("plan"), plan_children, rework=StartOver(), feedback_to=PLAN)
    delivery_children: list[Stage | Step | Parallel] = [Parallel(TASKS, [])]
    if rank >= 2:
        delivery_children.append(Step(INTEGRATION))
    delivery_children.append(Step(DESCRIBE))      # every tier: the PR, described and reviewed
    # a fix round runs before integration (or, without one, the description): both again after
    delivery = Stage(stage_id("delivery"), delivery_children,
                     Step(H_CHANGE, rejectable=True),
                     rework=AppendRound(round_template(rank, environment),
                                        before=INTEGRATION if rank >= 2 else DESCRIBE))
    stages = [requirements, plan, delivery]
    if publish:        # the source is on GitHub: the accepted change's pull request is opened
        stages.append(Stage(stage_id("publish"), [Step(PUBLISH)]))
    return Workflow(stages)


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
