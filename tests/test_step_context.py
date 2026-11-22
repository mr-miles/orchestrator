"""Where an agent's work stands (workflow/context.py, MCP step_status / workflow_context)."""
from __future__ import annotations

import pytest
from engine_harness import TASKS, Flow, scenario

from legion_orch.server import MCP_TOOLS
from legion_orch.workflow.engine import WorkflowError


def two_tasks():
    t2 = dict(TASKS[0], id="T2", title="farewell", files_modified=["src/bye.txt"],
              depends_on=["T1"], verification=["test -f src/bye.txt"])
    return [TASKS[0], t2]


@scenario()
async def test_step_status_shows_cycle_budget_verification_panel_and_agents(f: Flow):
    assert {"step_status", "workflow_context"} <= set(MCP_TOOLS)
    await f.to_build()
    s = f.ce.step_status("build:T1")
    assert s["status"] == "RUNNING" and s["cycle"] == 1
    assert s["budget"]["kind"] == "review cycles" and s["budget"]["left"] >= 1
    judges = f.ce.change.panel
    assert [p["role"] for p in s["panel"]] == judges and all(p["verdict"] is None for p in s["panel"])
    assert {"card": "implementer@build:T1", "state": "running"} in s["agents"]
    await f.implement()
    s = f.ce.step_status("build:T1")
    assert s["status"] == "REVIEWING" and s["verification"]["state"] == "passed"
    await f.review(role=judges[0], verdict="FAIL")
    s = f.ce.step_status("build:T1")
    if len(judges) == 1:                                    # the panel decided: rework
        assert s["status"] in ("REWORK", "RUNNING") and s["cycle"] == 2
    with pytest.raises(WorkflowError, match="unknown step"):
        f.ce.step_status("build:T9")


@scenario()
async def test_workflow_context_places_the_task_in_the_change(f: Flow):
    await f.to_build(tasks=two_tasks())
    ctx2 = f.ce.workflow_context("build:T2")
    assert ctx2["change"]["goal"] == f.ce.change.goal and any(s["here"] for s in ctx2["stages"])
    task = ctx2["task"]
    assert [d["task"] for d in task["builds_on"]] == ["T1"] and task["built_on_by"] == []
    assert [d["task"] for d in task["nearby"]] == ["T1"]                # both under src/
    assert task["nearby"][0]["shared_dirs"] == ["src"]
    assert ctx2["requirements"][0]["id"] == "REQ-1"
    assert ctx2["requirements"][0]["criteria"][0]["id"] == "AC-1"
    assert {j["role"] for j in ctx2["judged_by"]} == set(f.ce.change.panel)
    ask = {a["card"]: a["why"] for a in ctx2["ask"]}
    assert ask["orchestrator"].startswith("list_cards")
    assert ask.get("implementer@build:T1") == "your task builds on it"
    ctx1 = f.ce.workflow_context("build:T1")
    assert [d["task"] for d in ctx1["task"]["built_on_by"]] == ["T2"]
