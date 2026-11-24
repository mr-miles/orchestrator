"""Model and effort per role (config ``models``, WORKFLOW.md §2.3): resolved per run, passed to
the agent CLI, recorded with the model that served the run."""
from __future__ import annotations

import json

import pytest
from engine_harness import Flow, scenario

from legion_orch.config import ModelsConfig, agent_command
from legion_orch.workflow.engine.services.runs import model_matches


def test_a_run_takes_the_default_then_its_roles_then_its_souls():
    m = ModelsConfig.model_validate({
        "default": {"model": "sonnet", "effort": "medium"},
        "roles": {"reviewer": {"model": "opus"}, "planner": {"effort": "high"}},
        "souls": {"frontend-engineer": {"model": "haiku"}}})
    assert m.resolve("reviewer").model_dump() == {"model": "opus", "effort": "medium"}
    assert m.resolve("planner").model_dump() == {"model": "sonnet", "effort": "high"}
    assert m.resolve("implementer", "frontend-engineer").model == "haiku"
    assert ModelsConfig().resolve("reviewer").model_dump() == {"model": None, "effort": None}


def test_names_the_assets_do_not_have_are_refused():
    m = ModelsConfig.model_validate({"roles": {"reviewr": {}}, "souls": {"nobody": {}}})
    assert m.unknown(["reviewer"], ["code-reviewer"]) == [
        "models.roles: no role 'reviewr'", "models.souls: no soul 'nobody'"]
    with pytest.raises(ValueError):
        ModelsConfig.model_validate({"default": {"effort": "huge"}})


def test_the_agent_command_passes_them_only_when_set():
    from legion_orch.config import SandboxConfig
    cmd = agent_command(SandboxConfig())
    assert '${LEGION_MODEL:+--model "$LEGION_MODEL"}' in cmd
    assert '${LEGION_EFFORT:+--effort "$LEGION_EFFORT"}' in cmd


@pytest.mark.parametrize("asked, served, same", [
    ("opus", "claude-opus-5-5", True), ("claude-opus-5-5", "claude-opus-5-5", True),
    ("opus[1m]", "claude-opus-5-5[1m]", True), ("sonnet", "claude-opus-5-5", False),
])
def test_an_alias_matches_the_model_it_names(asked, served, same):
    assert model_matches(asked, served) is same


@scenario()
async def test_runs_ask_for_their_roles_model_and_record_what_served_them(f: Flow):
    f.cfg.models = ModelsConfig.model_validate({
        "default": {"model": "sonnet"}, "roles": {"reviewer": {"model": "opus", "effort": "high"}}})
    await f.to_build()
    impl = f.run("T1", "implementer")
    assert (impl.model, impl.effort) == ("sonnet", None)
    env = f.runner.payloads[impl.sandbox]["run.env"]
    assert "\nLEGION_MODEL=sonnet\n" in env and "\nLEGION_EFFORT=" not in env
    with open(f.ce.run_dir(impl.id) / "agent.log", "a") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init", "session_id": "s1",
                             "model": "claude-opus-5-5", "tools": []}) + "\n")
    await f.implement()
    assert impl.model_served == "claude-opus-5-5"
    assert "asked for model sonnet but was served by claude-opus-5-5" in f.ce.timeline()
    review = f.run("review:T1", "reviewer")
    assert (review.model, review.effort) == ("opus", "high")
    assert "LEGION_EFFORT=high" in f.runner.payloads[review.sandbox]["run.env"]
