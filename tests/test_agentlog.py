"""Agent logs: parsing sandbox output and Claude Code stream-json into display entries, the
agent's own result, and the operator API that serves them."""
from __future__ import annotations

import json

import httpx
from engine_harness import Flow, scenario

from legion_orch.sandbox import agentlog

INIT = {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": ["Bash", "Read"],
        "mcp_servers": [{"name": "legion", "status": "connected"}]}
CALL = {"type": "assistant", "message": {"content": [
    {"type": "thinking", "thinking": "Let me look."},
    {"type": "text", "text": "Fetching the assignment."},
    {"type": "tool_use", "id": "t1", "name": "mcp__legion__get_assignment", "input": {}},
    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls -la"}}]}}
RESULT = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "a\nb"}]},
    {"type": "tool_result", "tool_use_id": "t3", "content": "boom", "is_error": True}]}}
DONE = {"type": "result", "subtype": "success", "is_error": False, "result": "Reported.",
        "num_turns": 7, "duration_ms": 61000, "total_cost_usd": 0.42}


def log(*lines) -> str:
    return "".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines)


def test_entries_group_sandbox_output_and_render_agent_events():
    text = log("[legion] setup: run r1", "[legion] cloning http://h/git/app.git", "",
               INIT, CALL, RESULT, "stray stderr line", DONE)
    es = agentlog.entries(text)
    kinds = [e["kind"] for e in es]
    assert kinds == ["sandbox", "init", "thinking", "text", "tool_call", "tool_call",
                     "tool_result", "tool_result", "sandbox", "result"]
    assert es[0]["text"] == "[legion] setup: run r1\n[legion] cloning http://h/git/app.git"
    assert "MCP: legion connected" in es[1]["text"] and not es[1]["error"]
    assert (es[4]["title"], es[5]["title"], es[5]["text"]) == ("legion.get_assignment", "Bash", "ls -la")
    assert es[6]["text"] == "a\nb" and not es[6]["error"] and es[7]["error"]
    assert es[-1]["text"].startswith("Reported.") and "7 turns" in es[-1]["text"]
    assert "$0.42" in es[-1]["text"] and not es[-1]["error"]


def test_entries_flag_failed_mcp_and_error_results_and_clip_long_text():
    bad_init = INIT | {"mcp_servers": [{"name": "legion", "status": "failed"}]}
    err = {"type": "result", "subtype": "error_max_turns", "is_error": True}
    big = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 9000}]}}
    es = agentlog.entries(log(bad_init, big, err, {"type": "rate_limit_event"}, "{not json"))
    assert es[0]["error"] and "legion failed" in es[0]["text"]
    assert len(es[1]["text"]) < 4100 and "more characters" in es[1]["text"]
    assert es[2]["error"] and es[2]["title"] == "error_max_turns"
    assert es[3] == {"kind": "event", "title": "rate_limit_event", "text": "", "error": False}
    assert es[4]["kind"] == "sandbox"           # JSON-looking but invalid: shown as output


def test_agent_result_tells_agent_errors_from_none():
    assert agentlog.agent_result(log("bash: claude: command not found")) is None
    ok = agentlog.agent_result(log(INIT, DONE))
    assert ok and not ok.is_error and ok.message == "Reported."
    r = agentlog.agent_result(log({"type": "result", "subtype": "success", "is_error": True,
                                   "result": "API Error: 401"}))
    assert r.is_error and r.message == "API Error: 401"
    r = agentlog.agent_result(log({"type": "result", "subtype": "error_during_execution",
                                   "errors": ["tool crashed"]}))
    assert r.is_error and r.message == "tool crashed"
    assert agentlog.last_output(log("a", INIT, "b", "c", "d"), 2) == "c\nd"


@scenario()
async def test_log_api_serves_entries_with_stage_and_failure(f: Flow):
    from legion_orch.server.web import build_app
    r = f.run("intake")
    with open(f.ce.run_dir(r.id) / "agent.log", "a") as fh:
        fh.write(log(INIT, CALL))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(f.ctl)),
                                 base_url="http://op") as c:
        body = (await c.get(f"/api/runs/{r.id}/log?format=entries")).json()
        assert body["live"] and body["stage"] == "LAUNCHING" and body["failure"] is None
        assert [e["kind"] for e in body["entries"]][:3] == ["sandbox", "init", "thinking"]
        raw = await c.get(f"/api/runs/{r.id}/log")
        assert raw.headers["content-type"].startswith("text/plain") and '"type": "system"' in raw.text
        await f.exit_agent(r, rc=2)
        body = (await c.get(f"/api/runs/{r.id}/log?format=entries")).json()
        assert not body["live"] and body["failure"]["source"] == "SYSTEM"
        assert body["failure"]["exit_code"] == 2 and body["entries"]   # served from the store now


def test_run_agent_script_continues_a_pushed_branch_in_a_fresh_sandbox(tmp_path):
    """After a retry the sandbox is new: the working copy starts from what was pushed."""
    import shlex
    import subprocess

    from legion_orch.sandbox import RUN_AGENT_SH
    git = lambda *a, cwd=tmp_path: subprocess.run(["git", *a], cwd=cwd, check=True,
                                                 capture_output=True, text=True).stdout.strip()
    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", "-b", "main", cwd=src)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base",
        cwd=src)
    base = git("rev-parse", "HEAD", cwd=src)
    git("checkout", "-q", "-b", "legion/c1/build-t1", cwd=src)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "work",
        cwd=src)
    pushed = git("rev-parse", "HEAD", cwd=src)
    git("checkout", "-q", "main", cwd=src)
    for sandbox, branch, want in (("sb1", "legion/c1/build-t1", pushed), ("sb2", "legion/c1/new", base)):
        p = tmp_path / sandbox / "payload"
        p.mkdir(parents=True)
        env = {"LEGION_RUN_ID": "r1", "LEGION_ROLE": "implementer", "LEGION_BRANCH": branch,
               "LEGION_BASE_SHA": base, "LEGION_GIT_URL": str(src), "LEGION_NEEDS_REPO": "1",
               "LEGION_AGENT_COMMAND": "git rev-parse HEAD > ../head"}
        (p / "run.env").write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()))
        (p / "run-agent.sh").write_text(RUN_AGENT_SH)
        out = subprocess.run(["bash", str(p / "run-agent.sh")], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert "[legion] starting the agent" in out.stdout
        assert (tmp_path / sandbox / "head").read_text().strip() == want, branch


# Claude Code's events for a background task (Monitor / Bash run_in_background), as captured
STARTED = {"type": "system", "subtype": "task_started", "task_id": "bx1", "tool_use_id": "t9",
           "description": "run the test suite", "is_backgrounded": True, "task_type": "local_bash"}
SNAPSHOT = {"type": "system", "subtype": "background_tasks_changed",
            "tasks": [{"task_id": "bx1", "task_type": "local_bash", "description": "run the test suite"}]}
WAITING = {"type": "result", "subtype": "success", "is_error": False,
           "result": "Waiting for the test-suite monitor to finish."}
UPDATED = {"type": "system", "subtype": "task_updated", "task_id": "bx1",
           "patch": {"status": "completed", "end_time": 1}}
NOTIFIED = {"type": "system", "subtype": "task_notification", "task_id": "bx1", "tool_use_id": "t9",
            "status": "completed", "summary": "Monitor \"run the test suite\" stream ended"}
EMPTY = {"type": "system", "subtype": "background_tasks_changed", "tasks": []}
RESUMED = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Tests passed."}]}}
WAIT_TURN = (INIT, CALL, SNAPSHOT, STARTED, WAITING)


def test_a_result_while_background_tasks_run_ends_a_turn_not_the_run():
    r = agentlog.agent_result(log(*WAIT_TURN))
    assert r and not r.is_error and r.pending == ("run the test suite",)
    es = agentlog.entries(log(*WAIT_TURN))
    assert [e["kind"] for e in es][-2:] == ["task", "waiting"]
    assert es[-1]["text"].startswith("waiting on 1 background task: run the test suite")
    # the task finished and the CLI resumed: the second result ends the run
    full = log(*WAIT_TURN, UPDATED, NOTIFIED, EMPTY, INIT, RESUMED, DONE)
    r = agentlog.agent_result(full)
    assert r and r.message == "Reported." and r.pending == ()
    kinds = [e["kind"] for e in agentlog.entries(full)]
    assert kinds.count("waiting") == 1 and kinds[-1] == "result"


def test_a_resumed_turn_that_dies_has_no_result_and_stopped_tasks_are_not_pending():
    assert agentlog.agent_result(log(*WAIT_TURN, NOTIFIED, INIT, RESUMED)) is None
    killed = UPDATED | {"patch": {"status": "killed"}}
    assert agentlog.agent_result(log(INIT, STARTED, killed, DONE)).pending == ()
    # a log tail that missed task_started still sees the task through the snapshot
    assert agentlog.agent_result(log(SNAPSHOT, WAITING)).pending == ("run the test suite",)


@scenario()
async def test_cli_exiting_while_the_agent_waits_on_a_task_is_a_system_error(f: Flow):
    r = f.run("intake")
    with open(f.ce.run_dir(r.id) / "agent.log", "a") as fh:
        fh.write(log(*WAIT_TURN))
    await f.exit_agent(r, rc=0)
    assert r.failure.source == "SYSTEM"
    assert "waiting on 1 background task (run the test suite)" in r.failure.reason
