"""Producers (intake, planner, oracle) keep their sandbox, stopped between runs, and their agent
session across rework until the gate that can send their work back has passed."""
from __future__ import annotations

import json
import shlex
import subprocess

from engine_harness import Flow, scenario

from legion_orch.sandbox import RUN_AGENT_SH, agentlog
from legion_orch.workflow.model import Run


def give_session(f: Flow, run: Run) -> str:
    """The agent CLI's init event for ``run``: its session id."""
    sid = f"session-{run.id}"
    with open(f.ce.run_dir(run.id) / "agent.log", "a") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init", "session_id": sid}) + "\n")
    return sid


def resume_of(env: str) -> str | None:
    for line in env.splitlines():
        if line.startswith("LEGION_RESUME_SESSION="):
            return shlex.split(line.partition("=")[2])[0]
    return None


@scenario()
async def test_the_planner_keeps_sandbox_and_session_until_its_ticket_passes(f: Flow):
    await f.intake()
    await f.signoff("H-1")
    first = f.run("design")
    assert resume_of(f.runner.run_envs[-1]) is None          # nothing to resume yet
    sid = give_session(f, first)
    await f.plan()
    sb = first.sandbox
    assert sb in f.runner.stopped and sb not in f.runner.deleted
    assert f.ticket("design").sandbox == sb

    critic = f.run("critique", "critic")
    assert critic.sandbox != sb                              # judges stay fresh (G-D4)
    await f.critique("REWORK")
    second = f.run("design")
    assert second.id != first.id and second.sandbox == sb    # same sandbox, started again
    assert sb in f.runner.restarted and f.runner.created.count(sb) == 1
    assert resume_of(f.runner.run_envs[-1]) == sid           # the rework continues the session
    assert critic.sandbox in f.runner.deleted          # its judgement is in: released

    give_session(f, second)
    await f.plan()
    await f.critique("PASS")
    assert sb not in f.runner.deleted                      # H-E1 can still send the plan back
    await f.signoff("H-E1")
    assert sb in f.runner.deleted and f.ticket("design").sandbox is None


@scenario()
async def test_intake_keeps_its_session_until_h1_passes(f: Flow):
    first = f.run("intake")
    sid = give_session(f, first)
    await f.intake()
    assert first.sandbox in f.runner.stopped
    await f.signoff("H-1", approve=False, reason="also say goodbye")
    second = f.run("intake")
    assert second.sandbox == first.sandbox and resume_of(f.runner.run_envs[-1]) == sid
    assert "also say goodbye" in second.assignment["findings"]
    await f.intake()
    assert first.sandbox not in f.runner.deleted
    await f.signoff("H-1")
    assert first.sandbox in f.runner.deleted


@scenario()
async def test_the_oracle_keeps_its_sandbox_until_h2(f: Flow):
    from engine_harness import signals
    await f.intake(signals(scope_breadth=2, novelty_uncertainty=2, verification_quality=2))
    await f.signoff("H-1")
    await f.plan()
    await f.critique()
    await f.signoff("H-E1")
    await f.oracle()
    oracle = next(r for r in f.ce.runs.values() if r.role == "oracle")
    assert oracle.sandbox in f.runner.stopped and oracle.sandbox not in f.runner.deleted
    assert f.ticket("oracle").sandbox == oracle.sandbox
    await f.signoff("H-2")
    assert oracle.sandbox in f.runner.deleted


@scenario()
async def test_a_sandbox_that_cannot_stop_stays_running_and_is_reused(f: Flow):
    f.runner.fail_stop = True
    await f.intake()
    await f.signoff("H-1")
    first = f.run("design")
    await f.plan()
    await f.critique("REWORK")
    assert f.run("design").sandbox == first.sandbox and first.sandbox not in f.runner.restarted


def test_session_id_comes_from_the_agent_cli_events():
    log = "\n".join([
        "[legion] setup",
        json.dumps({"type": "system", "subtype": "init", "session_id": "abc-1"}),
        json.dumps({"type": "result", "subtype": "success", "session_id": "abc-1"}),
        json.dumps({"type": "system", "subtype": "init", "session_id": "bad id; rm -rf /"}),
    ])
    assert agentlog.session_id(log) == "abc-1"               # unsafe ids are ignored
    assert agentlog.session_id("no events") is None


def test_run_agent_script_resumes_only_a_session_the_sandbox_has(tmp_path):
    p = tmp_path / "sb" / "payload"
    p.mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".claude/projects/-sandbox-work").mkdir(parents=True)
    (home / ".claude/projects/-sandbox-work/kept.jsonl").write_text("{}\n")
    (p / "run-agent.sh").write_text(RUN_AGENT_SH)
    seen = {}
    for sid in ("kept", "gone"):
        env = {"LEGION_NEEDS_REPO": "0", "LEGION_RESUME_SESSION": sid,
               "LEGION_AGENT_COMMAND": 'echo "${LEGION_RESUME_SESSION:-none}" > ../resumed'}
        (p / "run.env").write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()))
        out = subprocess.run(["bash", str(p / "run-agent.sh")], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
        assert out.returncode == 0, out.stderr
        seen[sid] = (tmp_path / "sb" / "resumed").read_text().strip()
    assert seen == {"kept": "kept", "gone": "none"}           # a missing session starts afresh
