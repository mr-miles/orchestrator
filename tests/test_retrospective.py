"""What is recorded for a retrospective (whole timestamped logs, digests, template values, asset
hashes, the timeline), agents' reflections before their sandbox goes, and the on-demand
retrospective of an ended change."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys

import pytest
from engine_harness import ROOT, Flow, scenario

from legion_orch.sandbox import agentlog
from legion_orch.sandbox.runner import spawn_logged, stamp
from legion_orch.workflow.assets import Assets
from legion_orch.workflow.engine import WorkflowError
from legion_orch.workflow.engine.runs import RunLifecycle, split_lines
from legion_orch.workflow.model import ACCEPTED, ChangeStatus, Run


def say(f: Flow, run: Run, *events: dict) -> None:
    """The agent CLI writes stream-json events to its log."""
    with open(f.ce.run_dir(run.id) / "agent.log", "a") as fh:
        fh.writelines(json.dumps(e) + "\n" for e in events)


def init(sid: str) -> dict:
    return {"type": "system", "subtype": "init", "session_id": sid, "model": "m", "tools": []}


def tool(i: str, name: str, inp: dict, ts: str) -> dict:
    return {"type": "assistant", "legion_ts": ts,
            "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def result_of(i: str, ts: str, error: bool = False, text: str = "ok") -> dict:
    return {"type": "user", "legion_ts": ts, "message": {"content": [
        {"type": "tool_result", "tool_use_id": i, "is_error": error, "content": text}]}}


# ---------------------------------------------------------------------------- recording
def test_stamp_adds_the_arrival_time_to_events_only():
    out = stamp(b'{"type":"result"}')
    assert out.startswith(b'{"legion_ts":"') and json.loads(out)["type"] == "result"
    assert stamp(b"[legion] cloning") == b"[legion] cloning"


def test_the_controller_timestamps_agent_output_as_it_arrives(tmp_path):
    log = tmp_path / "agent.log"
    script = 'print("[legion] setup"); print(\'{"type": "system", "subtype": "init"}\'); exit(3)'

    async def go():
        proc = await spawn_logged([sys.executable, "-c", script], log)
        return await proc.wait()
    assert asyncio.run(go()) == 3
    setup, event = log.read_text().splitlines()
    assert setup == "[legion] setup" and json.loads(event)["legion_ts"].endswith("Z")


def test_split_lines_keeps_parts_under_the_limit():
    text = "".join(f"line {i} " + "é" * 20 + "\n" for i in range(200)) + "x" * 5000
    parts = split_lines(text, 1000)
    assert "".join(parts) == text and all(len(p.encode()) <= 1000 for p in parts)


def test_digest_summarises_tools_errors_repeats_and_timing():
    log = "\n".join(json.dumps(e) for e in [
        init("s1"),
        tool("a", "Bash", {"command": "pytest"}, "2026-01-01T00:00:00.000Z"),
        result_of("a", "2026-01-01T00:01:30.000Z", error=True, text="1 failed"),
        tool("b", "Bash", {"command": "pytest"}, "2026-01-01T00:02:00.000Z"),
        result_of("b", "2026-01-01T00:02:10.000Z"),
        tool("c", "mcp__legion__report", {"status": "DONE"}, "2026-01-01T00:05:00.000Z"),
        {"type": "result", "subtype": "success", "num_turns": 4, "total_cost_usd": 0.5},
    ]) + "\n[legion] done\n"
    d = agentlog.digest(log)
    assert d["agent"]["session_id"] == "s1" and d["wall_seconds"] == 300
    assert d["tools"]["Bash"] == {"calls": 2, "errors": 1} and "legion.report" in d["tools"]
    assert d["repeated_commands"] == [{"value": "pytest", "count": 2}]
    assert d["slowest_tool_calls"][0] == {"tool": "Bash", "input": "pytest", "seconds": 90.0}
    assert d["longest_model_waits"][0]["seconds"] == 170.0          # after the second result
    assert d["tool_errors"][0]["error"] == "1 failed" and d["results"][0]["num_turns"] == 4


def test_a_prompt_is_checked_against_the_current_asset_files(tmp_path):
    shutil.copytree(ROOT / "assets", tmp_path / "assets")
    a = Assets(tmp_path / "assets")
    values = {v: "x" for v in ("change_id", "goal", "tier", "repo_name", "base_sha", "head_sha",
                               "branch", "task_id", "task_title", "task_json", "cycle", "role",
                               "context_refs", "feedback", "candidate_sha", "write_scope",
                               "required_docs", "workdir", "task_soul", "environment")}
    pin, recorded = a.render_role("release", values).pin, a.manifest()
    assert a.check_prompt("release", values, pin, recorded) == {"status": "exact", "changed": []}
    soul = tmp_path / "assets/souls/release-planner.md"
    soul.write_text(soul.read_text() + "\n- Be brief.\n")
    later = Assets(tmp_path / "assets")
    assert later.check_prompt("release", values, pin, recorded) == {
        "status": "changed", "changed": ["souls/release-planner.md"]}


@scenario()
async def test_a_run_keeps_its_whole_log_digest_values_and_session(f: Flow):
    RunLifecycle.PART_BYTES, saved = 300, RunLifecycle.PART_BYTES
    try:
        intake = f.run("intake")
        say(f, intake, init("sess-1"), *[tool(str(i), "Read", {"file_path": "a.py"},
                                              "2026-01-01T00:00:00.000Z") for i in range(5)])
        whole = (f.ce.run_dir(intake.id) / "agent.log").read_text()
        await f.intake()
    finally:
        RunLifecycle.PART_BYTES = saved
    assert f.ce.latest(f"control/logs/{intake.id}.2.log")                 # stored in parts
    assert f.ce.stored_log(intake.id) == whole
    assert f.ce.log_tail(intake.id, 1) == whole.splitlines()[-1]
    digest = f.ce.json_doc(f"control/digests/{intake.id}.json")
    assert digest["repeated_reads"] == [{"value": "a.py", "count": 5}]
    assert intake.session_id == "sess-1"
    assert intake.template_vars["change_id"] == f.ce.cid and intake.template_vars["role"] == "intake"
    sha = intake.assets_sha.removeprefix("sha256:")[:16]
    assert f.ce.json_doc(f"control/assets/{sha}.json")["files"]["souls/intake-analyst.md"]
    assert f.ce.assets.check_prompt("intake", intake.template_vars, intake.prompt_pin)["status"] == "exact"
    kinds = [json.loads(line)["kind"] for line in f.ce.timeline().splitlines()]
    assert "dispatch" in kinds and "human" in kinds


@scenario()
async def test_a_rework_records_the_session_it_resumed(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    first = f.run("plan")
    say(f, first, init("plan-session"))
    await f.plan()
    await f.critique("REWORK")
    second = f.run("plan")
    assert (second.resumed_from, second.resumed_session) == (first.id, "plan-session")


# ---------------------------------------------------------------------------- reflection
@scenario()
async def test_an_agent_reflects_before_its_kept_sandbox_is_deleted(f: Flow):
    f.cfg.retrospective.reflect = True
    await f.intake()
    await f.human("H-1", "accept")
    plan = f.run("plan")
    say(f, plan, init("plan-session"))
    await f.plan()
    await f.critique("PASS")
    assert not f.runner.reflected                        # still kept: H-2 can send it back
    await f.human("H-2", "approve")
    await f.settle()
    (sb, prompt), = [x for x in f.runner.reflected if x[0] == plan.sandbox]
    assert "Do not call any tools" in prompt and "## Time sinks" in prompt
    assert sb in f.runner.restarted                      # it was stopped between runs
    env = next(e for e in reversed(f.runner.run_envs) if "LEGION_REQUIRE_SESSION" in e)
    assert "LEGION_RESUME_SESSION=plan-session" in env and "LEGION_NEEDS_REPO=0" in env
    doc = f.ce.doc(f"control/reflections/{plan.id}.md").content
    assert doc.startswith("# Reflection: planner on plan") and "The tests took long." in doc
    assert sb in f.runner.deleted


@scenario()
async def test_an_implementer_reflects_on_the_findings_against_its_work(f: Flow):
    f.cfg.retrospective.reflect = True
    await f.to_build("R1")
    first = f.run("build:T1", "implementer")
    say(f, first, init("impl-session"))
    await f.implement()
    await f.review(verdict="FAIL", title="greeting lacks a newline")
    second = f.run("build:T1", "implementer")
    say(f, second, init("impl-session"))
    await f.implement()
    await f.review(verdict="PASS")
    await f.settle()
    assert f.ce.change.status == ChangeStatus.READY
    prompt = next(p for sb, p in f.runner.reflected if sb == first.sandbox)
    assert "greeting lacks a newline" in prompt and "why you missed it" in prompt
    assert f.ce.latest(f"control/reflections/{second.id}.md")


@scenario()
async def test_no_reflection_without_a_session_or_when_turned_off(f: Flow):
    await f.intake()                                     # reflect is off in the harness
    await f.human("H-1", "accept")
    f.cfg.retrospective.reflect = True
    plan = f.run("plan")
    await f.plan()                                       # no session id in its log
    await f.critique("PASS")
    await f.human("H-2", "approve")
    assert not f.runner.reflected and plan.sandbox in f.runner.deleted


# ---------------------------------------------------------------------------- retrospective
@scenario()
async def test_a_retrospective_runs_on_demand_once_the_change_has_ended(f: Flow):
    with pytest.raises(WorkflowError, match="ended change"):
        await f.change_action("retrospective")
    await f.to_build("R1")
    impl = f.run("build:T1", "implementer")
    say(f, impl, init("impl"), tool("t", "Bash", {"command": "make"}, "2026-01-01T00:00:00.000Z"))
    await f.implement()
    await f.review(verdict="PASS")
    assert f.ce.change.status == ChangeStatus.READY and "retrospective" not in f.ce.nodes
    await f.change_action("retrospective", focus="why was the build slow?")
    run = f.run("retrospective")
    files = f.runner.payloads[run.sandbox]
    for rel in ("README.md", "runs.json", "nodes.json", "timeline.jsonl", "prompt-check.json",
                f"logs/{impl.id}.log", f"digests/{impl.id}.json", "docs/plan/taskgraph.json",
                "assets/roles.yaml", "assets/souls/retrospective-analyst.md"):
        assert f"retro/{rel}" in files, rel
    assert not any(k.startswith("retro/docs/control/logs/") for k in files)
    assert json.loads(files["retro/prompt-check.json"])[impl.id]["status"] == "exact"
    assert "why was the build slow?" in files["prompt.md"]
    with pytest.raises(WorkflowError, match="already"):
        await f.change_action("retrospective")
    doc = await f.write(run, "retro/report.md", "# Retrospective\n\n## Summary\n\nFine.\n")
    out = await f.report(run, [doc])
    assert out.accepted and f.status("retrospective") == ACCEPTED
    assert f.ce.change.status == ChangeStatus.READY                  # the verdict is unchanged
    await f.change_action("retrospective")                           # again: a new cycle
    assert f.ce.nodes["retrospective"].cycle == 2 and f.run("retrospective").id != run.id


@scenario()
async def test_the_review_ledger_shows_when_each_finding_was_first_raised(f: Flow):
    f.cfg.retrospective.reflect = True
    await f.to_build("R1")
    for cycle, (verdict, title) in enumerate([("FAIL", "no newline"), ("FAIL", "wrong greeting"),
                                              ("PASS", "")], start=1):
        await f.implement()
        rev = f.run("build:T1", "reviewer")
        say(f, rev, init(f"review-{cycle}"))                 # judges do not reflect by default
        await f.review(verdict=verdict, title=title)
    assert f.ce.change.status == ChangeStatus.READY
    assert not any(sb == rev.sandbox for sb, _ in f.runner.reflected)
    await f.change_action("retrospective")
    ledger = json.loads(f.runner.payloads[f.run("retrospective").sandbox]["retro/findings.json"])
    t1 = ledger["tasks"]["T1"]
    assert [c["verdicts"] for c in t1["candidates"]] == [{"reviewer": "FAIL"}, {"reviewer": "FAIL"},
                                                         {"reviewer": "PASS"}]
    assert all(c["verification"]["ok"] and c["sha"] for c in t1["candidates"])
    late = next(x for x in t1["findings"] if x["title"] == "wrong greeting")
    assert late["first_raised_cycle"] == 2 and late["raised_in_cycles"] == [2]
    assert late["earlier_reviews"] == [{"cycle": 1, "sha": t1["candidates"][0]["sha"],
                                        "verdict": "FAIL"}]      # a possible escape in cycle 1
    assert ledger["critique"] and ledger["critique"][0]["verdict"] == "PASS"


@scenario()
async def test_the_retrospective_starts_by_itself_once_reflections_are_done(f: Flow):
    assert type(f.cfg.retrospective)().auto                  # the default
    f.cfg.retrospective.auto = True
    await f.to_build("R1")
    await f.implement()
    f.ce._lifecycle.reflector.pending.add("still-reflecting")
    await f.review(verdict="PASS")
    assert f.ce.change.status == ChangeStatus.READY
    assert f.status("retrospective") == "READY" and not f.runs("retrospective")   # waits
    f.ce._lifecycle.reflector.pending.clear()
    await f.settle()
    run = f.run("retrospective")
    doc = await f.write(run, "retro/report.md", "# Retrospective\n\n## Summary\n\nFine.\n")
    await f.report(run, [doc])
    assert f.status("retrospective") == ACCEPTED and len(f.runs("retrospective")) == 1
