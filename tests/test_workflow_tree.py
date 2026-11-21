"""The change's tree (engine/workflow_tree.py) per tier, environment and repository, driven
through the workflow core with scripted steps: the order things run in, what waits for whom,
the task graph defined once the plan is accepted, and fix rounds."""
from __future__ import annotations

import pytest

from legion_orch.workflow.engine.workflow_tree import (
    PLAN_GATE,
    SCAFFOLD,
    TASKS,
    base_id,
    build_workflow,
    in_round,
    round_of,
    stage_id,
    task_steps,
)
from legion_orch.workflow.flow import ActionError, Gate, Stage, Step, walk
from legion_orch.workflow.flow.testing import ScriptedRuntime

GRAPH = {"tasks": [
    {"id": "T0", "title": "scaffold", "scaffold": True, "depends_on": []},
    {"id": "T1", "title": "a", "depends_on": ["T0"]},
    {"id": "T2", "title": "b", "depends_on": ["T0", "T1"]},
]}


def ids(wf) -> list[str]:
    return [n.id for s in wf.stages for n in walk(s)]


def run(rank: int, *, environment=True, blank_repo=False, steps=None, pre=None,
        hold=("plan",)) -> ScriptedRuntime:
    return ScriptedRuntime(build_workflow(rank, environment=environment, blank_repo=blank_repo),
                           steps or {}, pre or {}, hold=set(hold)).start()


def plan_accepted(rt: ScriptedRuntime, graph=GRAPH, n: int = 1) -> ScriptedRuntime:
    """The critic passed the plan: its tasks are known, then the plan step completes."""
    scaffold, tasks = task_steps(graph)
    if n == 1 and SCAFFOLD in rt.flow.nodes:
        rt.define(SCAFFOLD, scaffold)
    else:
        tasks = scaffold + tasks
    rt.define(in_round(TASKS, n), tasks)
    return rt.finish(in_round("plan", n))


# ---------------------------------------------------------------------------- shapes
def test_r3_with_an_environment_in_a_blank_repository():
    wf = build_workflow(3, environment=True, blank_repo=True)
    assert ids(wf) == [
        "stage:requirements", "intake", "H-1",
        "stage:plan",
        "stage:design", "plan", "H-E1",
        "stage:oracle", "provision", SCAFFOLD, "oracle", "preflight", "baseline", "H-2",
        PLAN_GATE,
        "stage:delivery", TASKS, "integration", "release", "H-3"]
    gates = {n.id: n for s in wf.stages for n in walk(s) if isinstance(n, Gate)}
    assert [g for g, n in gates.items() if n.automatic] == [PLAN_GATE]
    assert [g for g, n in gates.items() if n.preconditions] == ["H-2", "H-3"]
    assert [g for g, n in gates.items() if n.rejectable] == ["H-3"]


def test_r2_has_no_baseline_and_its_oracle_gate_no_preconditions():
    wf = build_workflow(2, environment=False, blank_repo=False)
    assert ids(wf)[3:] == ["stage:plan", "stage:design", "plan", "H-E1",
                           "stage:oracle", "oracle", "preflight", "H-2", PLAN_GATE,
                           "stage:delivery", TASKS, "integration", "H-3"]
    h2 = next(n for s in wf.stages for n in walk(s) if n.id == "H-2")
    assert not h2.preconditions and not h2.automatic


def test_low_tiers_have_no_oracle_stage_and_accept_the_change_automatically():
    wf = build_workflow(1, environment=False, blank_repo=False)
    assert ids(wf) == ["stage:requirements", "intake", "H-1", "stage:plan", "stage:design",
                       "plan", "H-E1", PLAN_GATE, "stage:delivery", TASKS, "H-3"]
    h3 = wf.stages[-1].gate
    assert h3.automatic and h3.preconditions          # the ship gates still hold it
    # with an environment, provisioning still needs its own (automatic) stage
    wf = build_workflow(0, environment=True, blank_repo=False)
    oracle = next(n for s in wf.stages for n in walk(s) if n.id == "stage:oracle")
    assert [c.id for c in oracle.children] == ["provision"] and oracle.gate.automatic


def test_task_steps_keep_dependencies_within_their_group():
    scaffold, tasks = task_steps(GRAPH)
    assert [(s.id, s.depends_on) for s in scaffold] == [("build:T0", [])]
    assert [(s.id, s.depends_on) for s in tasks] == [("build:T1", []),
                                                     ("build:T2", ["build:T1"])]
    assert all(s.overridable for s in scaffold + tasks)
    _, later = task_steps({"tasks": [*GRAPH["tasks"], {"id": "T3", "title": "c",
                                                       "depends_on": ["T2"]}]},
                          built={"T0", "T1", "T2"})
    assert [(s.id, s.depends_on) for s in later] == [("build:T3", [])]


def test_round_ids():
    assert in_round("H-2", 1) == "H-2" and in_round("H-2", 3) == "H-2@3"
    assert (round_of("oracle@2"), base_id("oracle@2")) == (2, "oracle")
    assert (round_of("build:T1"), base_id("build:T1")) == (1, "build:T1")
    assert stage_id("round", 2) == "stage:round@2"


# ---------------------------------------------------------------------------- running it
def test_an_r3_change_runs_stage_by_stage():
    rt = run(3, blank_repo=True, pre={"H-2": [("independent_baseline",), ()]})
    assert rt.waiting()[0].node == "H-1"
    rt.act("H-1", "approve")
    assert rt.status("plan") == "running" and rt.status("H-E1") == "pending"
    plan_accepted(rt)
    assert rt.waiting()[0].node == "H-E1"
    assert rt.status("provision") == "pending"           # nothing after a waiting gate runs
    rt.act("H-E1", "approve")
    order = [s.step for s in rt.log if hasattr(s, "step") and type(s).__name__ == "StartStep"]
    assert order[-5:] == ["provision", "build:T0", "oracle", "preflight", "baseline"]
    # H-2's preconditions: the baseline attestation is missing, then supplied
    assert [(w.node, w.reason) for w in rt.waiting()] == [("H-2", "preconditions")]
    rt.act("H-2", "evaluate")
    rt.act("H-2", "approve")
    assert rt.status(PLAN_GATE) == "done"
    assert rt.status("build:T2") == "done" and rt.status("release") == "done"
    assert rt.waiting()[0].node == "H-3"
    rt.act("H-3", "approve")
    assert rt.ended == "completed"


def test_tasks_start_in_dependency_order():
    rt = run(2, environment=False, hold=("plan", "build:T1"))
    rt.act("H-1", "approve")
    plan_accepted(rt, graph={"tasks": GRAPH["tasks"][1:]})
    rt.act("H-E1", "approve").act("H-2", "approve")
    assert (rt.status("build:T1"), rt.status("build:T2")) == ("running", "pending")
    rt.finish("build:T1")
    assert rt.status("build:T2") == "done" and rt.status("integration") == "done"


def test_a_low_tier_change_ends_at_its_ship_gates_without_a_human():
    rt = run(1, environment=False, pre={"H-3": [("ship_gates",), ()]})
    rt.act("H-1", "approve")
    plan_accepted(rt, graph={"tasks": GRAPH["tasks"][1:]})
    rt.act("H-E1", "approve")
    assert [(w.node, w.reason) for w in rt.waiting()] == [("H-3", "preconditions")]
    assert "approve" not in rt.actions("H-3")
    rt.act("H-3", "evaluate")
    assert rt.ended == "completed"


def test_tasks_cannot_be_redefined_once_they_started():
    rt = run(2, environment=False, hold=("plan", "build:T1"))
    rt.act("H-1", "approve")
    plan_accepted(rt, graph={"tasks": GRAPH["tasks"][1:]})
    rt.act("H-E1", "approve").act("H-2", "approve")
    with pytest.raises(ActionError, match="already started"):
        rt.define(TASKS, [Step("build:T9")])


# ---------------------------------------------------------------------------- rework
def failing_integration() -> ScriptedRuntime:
    rt = run(3, steps={"integration": ["escalate:integration_failed", "ok"]},
             hold=("plan", "plan@2"))
    rt.act("H-1", "approve")
    plan_accepted(rt, graph={"tasks": GRAPH["tasks"][1:]})
    rt.act("H-E1", "approve").act("H-2", "approve")
    assert rt.status("integration") == "escalated"
    return rt


def test_a_failed_integration_offers_send_back_to_everything_it_was_built_on():
    rt = failing_integration()
    assert rt.actions("integration") == [
        "answer", "retry", "send_back:stage:delivery", "send_back:stage:plan",
        "send_back:stage:requirements", "abandon"]


def test_sending_delivery_back_runs_a_fix_round_then_integration_again():
    rt = failing_integration()
    rt.act("integration", "send_back:stage:delivery", text="T2 breaks the API")
    rnd = rt.flow.nodes["stage:round@2"]
    assert isinstance(rnd, Stage)
    assert [n.id for n in walk(rnd)] == [
        "stage:round@2", "stage:design@2", "plan@2", "H-E1@2",
        "stage:oracle@2", "provision@2", "oracle@2", "preflight@2", "baseline@2", "H-2@2",
        "tasks@2", "round-2-approved"]
    assert rt.starts_of("plan@2")[0].feedback == ("T2 breaks the API",)
    assert rt.status("build:T1") == "done"                 # merged work stays
    rt.define("tasks@2", [Step("build:T3", overridable=True)])
    rt.finish("plan@2").act("H-E1@2", "approve").act("H-2@2", "approve")
    assert rt.status("build:T3") == "done"
    assert rt.attempt("integration") == 2 and rt.status("integration") == "done"


def test_sending_the_plan_back_from_a_round_drops_the_round():
    rt = failing_integration()
    rt.act("integration", "send_back:stage:delivery", text="fix it")
    rt.finish("plan@2")
    assert "send_back:stage:plan" in rt.actions("H-E1@2")
    rt.act("H-E1@2", "send_back:stage:plan", text="start again")
    assert "stage:round@2" not in rt.flow.nodes and "plan@2" not in rt.flow.nodes
    assert rt.attempt("plan") == 2 and rt.starts_of("plan")[-1].feedback == ("start again",)
    assert rt.status("build:T1") == "pending" and rt.status("integration") == "pending"


def test_a_task_defined_again_after_a_send_back_continues_its_attempts():
    rt = failing_integration()
    assert rt.attempt("build:T1") == 1
    rt.act("integration", "send_back:stage:plan", text="again")
    plan_accepted(rt, graph={"tasks": GRAPH["tasks"][1:]})
    rt.act("H-E1", "approve").act("H-2", "approve")
    assert rt.attempt("build:T1") == 2
