"""What is recorded for a retrospective (whole timestamped logs, digests, template values, asset
hashes, the timeline), agents' reflections before their sandbox goes, and the retrospective –
the workflow's on-complete process – of an ended change."""
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
from legion_orch.workflow.engine.services.runs import RunLifecycle, split_lines
from legion_orch.workflow.model import ChangeStatus, Run


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
                               "oracle_sha", "change_base_sha", "branch", "ticket_id",
                               "ticket_goal", "work_json", "attempt", "role", "context_refs",
                               "findings", "judged_sha", "write_scope", "outputs", "workdir",
                               "sandbox_tools", "task_soul", "environment", "scaffold")}
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
    assert f.ce.latest(f"control/runs/{intake.id}/log.2")                 # stored in parts
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
    assert "dispatch" in kinds and "signoff" in kinds


async def to_critique(f: Flow) -> Run:
    await f.intake()
    await f.signoff("H-1")
    return f.run("design")


@scenario()
async def test_a_rework_records_the_session_it_resumed(f: Flow):
    first = await to_critique(f)
    say(f, first, init("plan-session"))
    await f.plan()
    await f.critique("REWORK")
    second = f.run("design")
    assert (second.resumed_from, second.resumed_session) == (first.id, "plan-session")


# ---------------------------------------------------------------------------- reflection
@scenario()
async def test_an_agent_reflects_before_its_kept_sandbox_is_deleted(f: Flow):
    f.cfg.retrospective.reflect = True
    plan = await to_critique(f)
    say(f, plan, init("plan-session"))
    await f.plan()
    await f.critique("PASS")
    assert not f.runner.reflected                        # still kept: H-E1 can send it back
    await f.signoff("H-E1")
    await f.settle()
    (sb, prompt), = [x for x in f.runner.reflected if x[0] == plan.sandbox]
    assert "Your only tool in this turn is the Skill tool" in prompt and "## Time sinks" in prompt
    # it brings no skills: the sandbox keeps those its run installed (run-agent.sh), so the
    # reflection reopens exactly what the run was given
    assert f.runner.reflection_skills[sb] == []
    assert any(k.startswith("skills/legion-") for k in f.runner.payloads[plan.sandbox])
    assert sb in f.runner.restarted                      # it was stopped between runs
    env = next(e for e in reversed(f.runner.run_envs) if "LEGION_REQUIRE_SESSION" in e)
    assert "LEGION_RESUME_SESSION=plan-session" in env and "LEGION_NEEDS_REPO=0" in env
    assert "--tools" in env                              # a reflection has no tools to call
    work = next(e for e in f.runner.run_envs if "LEGION_RUN_ID=" + plan.id + "\n" in e)
    assert "--tools" not in work                         # the work run keeps them
    doc = f.ce.doc(f"control/reflections/{plan.id}.md").content
    assert doc.startswith("# Reflection: planner on design") and "The tests took long." in doc
    assert sb in f.runner.deleted


@scenario(environment={})
async def test_the_planner_reflects_once_h_e1_approves_its_plan_and_environment(f: Flow):
    """The planner's sandbox is kept until H-E1 approves the plan (with its environment); then
    it reflects as usual – provisioning, which maps the build roles, does not touch it."""
    f.cfg.retrospective.reflect = True
    plan = await to_critique(f)
    say(f, plan, init("plan-session"))
    await f.plan()
    await f.critique("PASS")
    assert f.ticket("design").sandbox == plan.sandbox and not f.runner.reflected
    await f.signoff("H-E1")
    await f.settle()
    assert f.status("provision") == "passed", f.status()
    assert "planner" not in f.ce.change.environment.roles
    assert [sb for sb, _ in f.runner.reflected] == [plan.sandbox]
    assert plan.sandbox in f.runner.restarted          # started again to reflect
    assert f.ce.latest(f"control/reflections/{plan.id}.md")
    assert plan.sandbox in f.runner.deleted


@scenario()
async def test_an_implementer_reflects_on_the_findings_against_its_work(f: Flow):
    f.cfg.retrospective.reflect = True
    await f.to_build()
    first = f.run("T1", "implementer")
    say(f, first, init("impl-session"))
    await f.implement()
    await f.review(verdict="FAIL", title="greeting lacks a newline")
    second = f.run("T1", "implementer")
    say(f, second, init("impl-session"))
    await f.to_end_from_build()
    await f.settle()
    assert f.ce.change.status == ChangeStatus.READY
    prompt = next(p for sb, p in f.runner.reflected if sb == first.sandbox)
    assert "greeting lacks a newline" in prompt and "why you missed it" in prompt
    assert f.ce.latest(f"control/reflections/{second.id}.md")


@scenario()
async def test_an_implementer_without_findings_is_not_asked_about_them(f: Flow):
    f.cfg.retrospective.reflect = True
    await f.to_build()
    impl = f.run("T1", "implementer")
    say(f, impl, init("impl-1"))
    await f.to_end_from_build()
    await f.settle()
    (_, prompt), = [x for x in f.runner.reflected if x[0] == impl.sandbox]
    assert "No reviewer, verification or human finding was raised" in prompt
    assert "listed above" not in prompt


@scenario()
async def test_a_resume_that_does_not_take_is_flagged(f: Flow):
    """The agent CLI ran in a new session instead of resuming the recorded one: the reflection
    says so and the timeline notes it."""
    f.cfg.retrospective.reflect = True
    f.runner.reflection_session = "fresh-session"
    await f.to_build()
    impl = f.run("T1", "implementer")
    say(f, impl, init("impl-1"))
    await f.to_end_from_build()
    await f.settle()
    doc = f.ce.doc(f"control/reflections/{impl.id}.md").content
    assert "> The agent did not resume session impl-1: it ran in a new one (fresh-session)" in doc
    assert f"run {impl.id}: the agent did not resume session impl-1" in f.ce.timeline()


@scenario()
async def test_a_rework_that_does_not_continue_its_session_is_noted(f: Flow):
    first = await to_critique(f)
    say(f, first, init("plan-1"))
    await f.plan()
    await f.critique("REWORK")
    second = f.run("design")
    assert second.id != first.id and second.resumed_session == "plan-1"
    say(f, second, init("plan-2"))
    await f.plan()
    assert (f"run {second.id} was to continue session plan-1 but ran in plan-2"
            in f.ce.timeline())


@scenario()
async def test_an_empty_answer_is_no_reflection(f: Flow):
    f.cfg.retrospective.reflect = True
    f.runner.reflection_text = "  \n"
    await f.to_build()
    impl = f.run("T1", "implementer")
    say(f, impl, init("impl-1"))
    await f.to_end_from_build()
    await f.settle()
    assert not f.ce.latest(f"control/reflections/{impl.id}.md")
    assert f"no reflection from run {impl.id}" in f.ce.timeline()


@scenario()
async def test_no_reflection_without_a_session_or_when_turned_off(f: Flow):
    await f.intake()                                     # reflect is off in the harness
    await f.signoff("H-1")
    f.cfg.retrospective.reflect = True
    plan = f.run("design")
    await f.plan()                                       # no session id in its log
    await f.critique("PASS")
    await f.signoff("H-E1")
    await f.settle()
    assert not f.runner.reflected and plan.sandbox in f.runner.deleted
    assert f"sandbox {plan.sandbox}: no agent session recorded" in f.ce.timeline()


# ---------------------------------------------------------------------------- retrospective
async def report_retro(f: Flow, run: Run):
    doc = await f.write(run, "report.md", "# Retrospective\n\n## Summary\n\nFine.\n")
    return await f.report(run, [doc], outputs={"report": doc})


@scenario()
async def test_a_retrospective_runs_on_demand_once_the_change_has_ended(f: Flow):
    with pytest.raises(WorkflowError, match="ended change"):
        await f.change_action("retrospective")
    await f.to_build()
    impl = f.run("T1", "implementer")
    say(f, impl, init("impl"), tool("t", "Bash", {"command": "make"}, "2026-01-01T00:00:00.000Z"))
    await f.to_end_from_build()
    assert f.ce.change.status == ChangeStatus.READY and not f.runs("retrospective")
    await f.change_action("retrospective", focus="why was the build slow?")
    await f.settle()
    run = f.run("retrospective")
    files = f.runner.payloads[run.sandbox]
    for rel in ("README.md", "runs.json", "tickets.json", "timeline.jsonl", "prompt-check.json",
                f"logs/{impl.id}.log", f"digests/{impl.id}.json", "docs/tickets/design/tickets.json",
                "assets/roles.yaml", "assets/souls/retrospective-analyst.md"):
        assert f"retro/{rel}" in files, rel
    assert not any(k.startswith("retro/docs/control/runs/") for k in files)
    assert json.loads(files["retro/prompt-check.json"])[impl.id]["status"] == "exact"
    assert "why was the build slow?" in run.instructions      # its task's first message
    assert "inbox()" in files["prompt.md"]
    with pytest.raises(WorkflowError, match="already"):
        await f.change_action("retrospective")
    out = await report_retro(f, run)
    assert out.accepted and f.status("retrospective") == "passed"
    assert f.ce.change.status == ChangeStatus.READY and f.ce.change.completed
    await f.change_action("retrospective")                           # again: another attempt
    await f.settle()
    assert f.ticket("retrospective").attempt == 2 and f.run("retrospective").id != run.id


@scenario()
async def test_the_review_ledger_shows_when_each_finding_was_first_raised(f: Flow):
    await f.to_build()
    for verdict, title in [("FAIL", "no newline"), ("FAIL", "wrong greeting")]:
        await f.implement()
        await f.review(verdict=verdict, title=title)
    await f.to_end_from_build()
    assert f.ce.change.status == ChangeStatus.READY
    await f.change_action("retrospective")
    await f.settle()
    ledger = json.loads(f.runner.payloads[f.run("retrospective").sandbox]["retro/findings.json"])
    t1 = ledger["tickets"]["T1"]
    assert [r["verdicts"] for r in t1["rounds"]] == [
        {"review:T1": "FAIL"}, {"review:T1": "FAIL"}, {"review:T1": "PASS"}]
    assert all(r["sha"] for r in t1["rounds"])
    late = next(x for x in t1["findings"] if x["title"] == "wrong greeting")
    assert (late["first_round"], late["rounds"]) == (2, [2])         # an escape in round 1?
    assert ledger["tickets"]["design"]["rounds"][0]["verdicts"] == {"critique": "PASS"}


@scenario()
async def test_the_retrospective_starts_by_itself_once_reflections_are_done(f: Flow):
    assert type(f.cfg.retrospective)().auto                  # the default
    f.cfg.retrospective.auto = True
    await f.to_build()
    reflector = f.ce._sandboxes.reflector
    reflector.pending.add("still-reflecting")
    await f.to_end_from_build()
    await f.settle()
    assert f.ce.change.status == ChangeStatus.READY and not f.ce.change.completed
    assert f.status("retrospective") == "working" and not f.runs("retrospective")   # waits
    reflector.pending.clear()
    f.ce.notify()                                            # as a finished reflection does
    await f.settle()
    await report_retro(f, f.run("retrospective"))
    assert f.status("retrospective") == "passed" and len(f.runs("retrospective")) == 1
    assert f.ce.change.completed and f.ce.change.cleaned_at          # then cleaned up


@scenario()
async def test_a_restart_runs_the_retrospective_again_at_the_same_attempt(f: Flow):
    f.cfg.retrospective.auto = True
    await f.to_end()
    await f.settle()
    first = f.run("retrospective")
    ctl, ce = await f.restart(recover=True)
    f.ctl, f.ce = ctl, ce
    await f.settle()
    again = f.run("retrospective")
    assert again.id != first.id and f.ticket("retrospective").attempt == 1
    await report_retro(f, again)
    assert f.ce.change.completed


# ---------------------------------------------------------------------------- abandoning
@scenario()
async def test_abandoning_records_the_reason_and_runs_the_retrospective(f: Flow):
    """Abandoned early (at intake): the reason is recorded, and the retrospective runs on what
    exists so far."""
    f.cfg.retrospective.auto = True
    r = f.run("intake")
    await f.report(r, [], status="BLOCKED",
                   blocked_reason="the goal is unclear", missing_decision="which game?")
    assert f.status("intake") == "waiting"
    await f.change_action("abandon", reason="wrong goal: we wanted chess, not Connect 4")
    await f.settle()
    ch = f.ce.change
    assert ch.status == ChangeStatus.ABANDONED
    assert (ch.ended.by, ch.ended.node, ch.ended.action) == ("miles", "intake", "abandon")
    assert ch.ended.reason == "wrong goal: we wanted chess, not Connect 4"
    assert f.ce.json_doc("control/ended.json")["reason"] == ch.ended.reason
    assert "abandoned the change at intake: wrong goal" in f.ce.timeline()
    run = f.run("retrospective")
    files = f.runner.payloads[run.sandbox]
    assert json.loads(files["retro/change.json"])["ended"]["reason"] == ch.ended.reason
    assert "`change.json` `ended`" in run.instructions


@scenario()
async def test_abandoning_can_skip_the_retrospective(f: Flow):
    f.cfg.retrospective.auto = True
    r = f.run("intake")
    await f.report(r, [], status="BLOCKED", blocked_reason="x", missing_decision="y")
    await f.change_action("abandon", retrospective=False)
    await f.settle()
    assert f.ce.change.ended.reason is None and not f.runs("retrospective")
    assert f.ce.change.completed and "(no reason given)" in f.ce.timeline()
