"""The workflow runtime (engine/runtime.py): how the engine and the workflow core stay in step –
what the engine's nodes tell the core, how the core's gates show on the gate nodes, what a human's
action does through the core, and that the core is rebuilt exactly from its record."""
from __future__ import annotations

import pytest
from engine_harness import Flow, scenario

from legion_orch.server import views
from legion_orch.workflow.engine import WorkflowError
from legion_orch.workflow.model import (
    ACCEPTED,
    AWAITING_HUMAN,
    ESCALATED,
    FAILED,
    PENDING,
    ChangeStatus,
)


def core(f: Flow, nid: str) -> str:
    return f.ce.workflow.status(nid)


def same_core(a, b) -> None:
    assert {n: a.workflow.status(n) for n in a.workflow.flow.state} == \
        {n: b.workflow.status(n) for n in b.workflow.flow.state}
    assert {n: a.workflow.flow.attempt(n) for n in a.workflow.flow.state} == \
        {n: b.workflow.flow.attempt(n) for n in b.workflow.flow.state}


# ---------------------------------------------------------------------------- engine -> core
@scenario()
async def test_an_accepted_node_completes_its_step_and_the_next_starts(f: Flow):
    assert core(f, "intake") == "running" and core(f, "H-1") == "pending"
    await f.intake()
    assert core(f, "intake") == "done" and core(f, "H-1") == "escalated"
    assert f.status("H-1") == AWAITING_HUMAN                 # a gate step: a decision is due


@scenario()
async def test_an_escalation_and_its_resolution_reach_the_core(f: Flow):
    await f.exit_agent(f.run("intake"), rc=1)
    assert f.status("intake") == ESCALATED and core(f, "intake") == "escalated"
    assert f.ce.workflow.actions("intake")[:2] == ["answer", "retry"]
    await f.human("intake", "retry")
    assert core(f, "intake") == "running"                    # the engine retries; the core resumes
    await f.intake()
    assert core(f, "intake") == "done"


# ---------------------------------------------------------------------------- gates
@scenario()
async def test_a_gate_is_a_step_whose_approval_answers_its_escalation(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    assert f.status("H-1") == ACCEPTED and core(f, "H-1") == "done"
    assert f.status("H-E1") == PENDING and core(f, "plan") == "running"
    await f.plan()
    await f.critique()
    assert f.status("H-E1") == AWAITING_HUMAN and core(f, "H-E1") == "escalated"
    assert f.ce.nodes["H-E1"].escalation.kind == "awaiting_approval"
    # stages without a human decision have no gate: the plan's own, and the oracle's at R1
    assert "H-2" not in f.ce.nodes
    await f.human("H-E1", "approve")
    assert core(f, "stage:plan") == "done" and core(f, "build:T1") == "running"


@scenario()
async def test_the_actions_a_node_offers_come_from_the_core(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    await f.critique()
    wf = views.workflow_state(f.ce)
    assert wf["actions"]["H-E1"] == ["answer", "retry", "send_back:stage:design",
                                     "send_back:stage:plan", "send_back:stage:requirements",
                                     "abandon"]
    assert set(wf["actions"]) == {"H-E1"}                 # nothing else waits for a human
    with pytest.raises(WorkflowError, match="cannot send stage:delivery back"):
        await f.human("H-E1", "send_back", stage="stage:delivery", feedback="x")
    with pytest.raises(WorkflowError, match="feedback is required"):
        await f.human("H-E1", "send_back", stage="design")


# ---------------------------------------------------------------------------- human actions
@scenario()
async def test_abandoning_ends_the_workflow_and_starts_nothing_over(f: Flow):
    await f.exit_agent(f.run("intake"), rc=1)
    await f.human("intake", "abandon", reason="a duplicate of another change")
    assert f.ce.workflow.flow.ended == "abandoned"
    assert f.ce.change.status == ChangeStatus.ABANDONED and f.status("intake") == FAILED
    assert f.ce.workflow.actions("intake") == [] and not f.runs("intake", "intake")[1:]


@scenario()
async def test_a_send_back_restarts_the_stage_with_its_feedback(f: Flow):
    await f.intake()
    await f.human("H-1", "revise", feedback="also say goodbye")
    intake = f.ce.nodes["intake"]
    assert core(f, "intake") == "running" and core(f, "H-1") == "pending"
    assert intake.cycle == 2 and intake.feedback[-1].text == "also say goodbye"
    assert intake.feedback[-1].attestations                 # the feedback document
    assert f.status("H-1") == PENDING


# ---------------------------------------------------------------------------- persistence
@scenario()
async def test_the_core_is_rebuilt_exactly_from_its_record(f: Flow):
    same_core(f.ce, (await f.restart())[1])                             # requirements only
    await f.intake()
    await f.human("H-1", "revise", feedback="again")
    await f.intake()
    await f.human("H-1", "accept", tier="R2")                # the tree is rebuilt for R2
    same_core(f.ce, (await f.restart())[1])
    await f.plan()
    await f.critique()
    again = (await f.restart())[1]
    same_core(f.ce, again)
    assert again.workflow.actions("H-E1") == f.ce.workflow.actions("H-E1")
    assert again.workflow.flow.attempt("intake") == 2        # attempts carried into the R2 tree


# ---------------------------------------------------------------------------- the graph shown
@scenario()
async def test_each_node_depends_on_what_runs_just_before_it(f: Flow):
    from engine_harness import TASKS
    t2 = dict(TASKS[0], id="T2", title="two", depends_on=["T1"], files_modified=["src/two.txt"])
    t3 = dict(TASKS[0], id="T3", title="three", files_modified=["src/three.txt"])
    await f.intake()
    await f.human("H-1", "accept", tier="R2")
    await f.plan(tasks=[TASKS[0], t2, t3])
    await f.critique()
    dep = {k: n.depends_on for k, n in f.ce.nodes.items()}
    assert dep["H-1"] == ["intake"] and dep["plan"] == ["H-1"] and dep["H-E1"] == ["plan"]
    assert dep["oracle"] == ["H-E1"] and dep["H-2"] == ["preflight"]
    assert dep["build:T1"] == ["H-2"] and dep["build:T2"] == ["build:T1"]
    assert dep["build:T3"] == ["H-2"]
    assert sorted(dep["integration"]) == ["build:T2", "build:T3"]     # the tasks' ends
    assert dep["describe"] == ["integration"] and dep["H-3"] == ["describe"]


@scenario()
async def test_the_stages_are_listed_with_their_nodes(f: Flow):
    await f.intake()
    await f.human("H-1", "accept", tier="R2")
    await f.plan()
    await f.critique()
    stages = {s["id"]: s for s in views.workflow_state(f.ce)["stages"]}
    assert list(stages) == ["stage:requirements", "stage:plan", "stage:design", "stage:oracle",
                            "stage:delivery"]
    assert stages["stage:design"]["nodes"] == ["plan", "H-E1"]
    assert stages["stage:design"]["title"] == "Design" and stages["stage:design"]["depth"] == 1
    assert stages["stage:plan"]["level"] == 1 and stages["stage:oracle"]["level"] == 0
    assert stages["stage:plan"]["nodes"] == ["plan", "H-E1", "oracle", "preflight", "H-2"]
    assert stages["stage:delivery"]["nodes"] == ["build:T1", "integration", "describe", "H-3"]
    assert stages["stage:design"]["status"] == "running"
