"""End-to-end: real HTTP server (MCP + git smart-HTTP + API), LocalRunner, scripted agents."""
import asyncio
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import uvicorn

from legion_orch.config import load_config
from legion_orch.engine import Engine
from legion_orch.web import build_app

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_repo(path: Path) -> None:
    path.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (path / "src").mkdir()
    (path / "src" / "greet.txt").write_text("hi\n")
    (path / "README.md").write_text("demo\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")


async def _wait(engine, pred, what, timeout=90):
    for _ in range(int(timeout / 0.2)):
        if pred():
            return
        await asyncio.sleep(0.2)
    events = "\n".join(f"{e['kind']} {e['node']} {e['message']}" for e in engine.st.events(limit=60))
    states = {n.id: n.status for n in engine.nodes.values()}
    raise AssertionError(f"timeout waiting for {what}; nodes={states}\n{events}")


def test_full_workflow(tmp_path):
    asyncio.run(_full_workflow(tmp_path))


async def _full_workflow(tmp_path):
    _make_repo(tmp_path / "src-repo")
    port = _free_port()
    cfg_path = tmp_path / "orchestrator.yaml"
    cfg_path.write_text(textwrap.dedent(f"""
        project: e2e
        repo: {{name: app, path: {tmp_path / 'src-repo'}, base_branch: main}}
        decision_owners: [miles]
        state_dir: state
        assets_dir: {ROOT / 'assets'}
        workflow_doc: {ROOT / 'WORKFLOW.md'}
        server: {{host: 127.0.0.1, port: {port}, public_url: "http://127.0.0.1:{port}"}}
        limits: {{max_parallel: 3, review_max_cycles: 3, critique_max_cycles: 2, run_timeout_s: 120}}
        sandbox:
          runner: local
          policy_template: {ROOT / 'policies/agent-policy.template.yaml'}
          verifier_policy_template: {ROOT / 'policies/verifier-policy.template.yaml'}
          agent_command: "{sys.executable} {ROOT / 'tests/fake_agent.py'}"
    """))
    cfg = load_config(cfg_path)
    engine = Engine(cfg)
    server = uvicorn.Server(uvicorn.Config(build_app(engine), host="127.0.0.1", port=port,
                                           log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        await _wait(engine, lambda: server.started, "server start", 20)
        await engine.start_change("Say hello and goodbye")
        n = engine.nodes

        await _wait(engine, lambda: n["H-1"].status == "AWAITING_HUMAN", "H-1")
        assert engine.change.risk["tier"] == "R2"          # public_api=2 floor
        await engine.human_action("H-1", "accept", "miles")
        assert engine.change.panel == ["reviewer"]

        await _wait(engine, lambda: n["H-2"].status == "AWAITING_HUMAN", "H-2")
        assert n["plan"].cycle == 2                         # critique forced one rework
        assert {"build:T1", "build:T2", "build:T3"} <= set(n)
        assert engine.change.oracle_sha
        with pytest.raises(Exception):
            await engine.human_action("H-2", "approve", "mallory")   # not a decision owner
        await engine.human_action("H-2", "approve", "miles")

        await _wait(engine, lambda: n["H-3"].status == "AWAITING_HUMAN", "H-3", 150)
        assert n["build:T1"].cycle == 2                     # review FAIL -> rework
        assert n["build:T2"].cycle == 2                     # verification FAIL -> rework
        t3_runs = [engine.runs[r] for r in n["build:T3"].runs]
        assert any(any(v.startswith("G-B3") for v in vs) for r in t3_runs for vs in r.violations)
        await engine.human_action("H-3", "accept", "miles")

        await _wait(engine, lambda: engine.change.ship is not None, "ship")
        ship = engine.change.ship
        assert ship["verdict"] == "READY", ship["gates"]
        assert set(ship["gates"]) == {
            "approved_delta_spec", "protected_oracle", "task_contract", "deterministic_verification",
            "task_level_independent_review", "integration_or_real_interface_checks",
            "whole_change_acceptance_evidence"}

        # every accepted run's sandbox is gone; documents are versioned & immutable
        await _wait(engine, lambda: all(r.sandbox_deleted for r in engine.runs.values()
                                        if r.status == "ACCEPTED"), "sandbox cleanup", 20)
        sandboxes = list((cfg.state_dir / "local-sandboxes").glob("*")) \
            if (cfg.state_dir / "local-sandboxes").exists() else []
        assert sandboxes == [], sandboxes
        assert engine.docs.latest_version("plan/spec.md") == 2
        with pytest.raises(Exception):
            engine.conn.execute("UPDATE documents SET content='x'")

        # integration branch contains all work; protected test unchanged
        head = engine.change.integration_head
        show = lambda p: subprocess.run(["git", "show", f"{head}:{p}"], cwd=engine.git.repo,
                                        capture_output=True, text=True).stdout
        assert show("src/greet.txt") == "hello\n\n"
        assert show("src/bye.txt") == "goodbye\n"
        assert "goodbye" in show("docs/README.md")
    finally:
        server.should_exit = True
        await task
