"""End-to-end: real HTTP server (MCP + git smart-HTTP + API), LocalRunner, scripted agents."""
import asyncio
import dataclasses
import json
import socket
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from legion_orch.config import load_config
from legion_orch.identity import WorkloadBinding
from legion_orch.server import build_servers
from legion_orch.server.views import change_state
from legion_orch.workflow.engine import Controller, WorkflowError

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
    states = {n.id: n.status for n in engine.nodes.values()} if hasattr(engine, "nodes") else {}
    raise AssertionError(f"timeout waiting for {what}; nodes={states}")


def _config(tmp_path, port):
    cfg_path = tmp_path / "orchestrator.yaml"
    cfg_path.write_text(textwrap.dedent(f"""
        project: e2e
        repo: {{name: app, path: {tmp_path / 'src-repo'}, base_branch: main}}
        decision_owners: [miles, ana]
        state_dir: state
        assets_dir: {ROOT / 'assets'}
        workflow_doc: {ROOT / 'WORKFLOW.md'}
        server: {{host: 127.0.0.1, port: {port}, agent_host: 127.0.0.1, public_host: 127.0.0.1,
                  mcp_port: {_free_port()}, git_port: {_free_port()}, token_port: {_free_port()}}}
        limits: {{max_parallel: 4, review_max_cycles: 3, critique_max_cycles: 2, run_timeout_s: 120}}
        sandbox:
          runner: local
          templates_dir: {ROOT / 'policies'}
          agent_command: "{sys.executable} {ROOT / 'tests/fake_agent.py'}"
    """))
    return load_config(cfg_path)


def test_full_workflow(tmp_path):
    asyncio.run(_full_workflow(tmp_path))


def test_r3_governance(tmp_path):
    asyncio.run(_r3_workflow(tmp_path))


async def _full_workflow(tmp_path):
    _make_repo(tmp_path / "src-repo")
    port = _free_port()
    cfg = _config(tmp_path, port)
    ctl = Controller(cfg)
    servers = await build_servers(ctl, log_level="warning")
    task = asyncio.gather(*(srv.serve() for srv in servers))
    server = servers[0]
    try:
        await _wait(ctl, lambda: server.started, "server start", 20)
        # two changes share one controller / state_dir and run concurrently
        a = await ctl.start_change("Say hello and goodbye")
        b = await ctl.start_change("Say hello and goodbye, again")
        assert a.cid != b.cid
        await asyncio.gather(_drive(a), _drive(b))
        old_token = ctl.auth.identity.tokens.issue(
            WorkloadBinding(sandbox="s", change=a.cid, task="intake", role="intake"), "x")
        await _identity_checks(ctl, cfg)
    finally:
        for srv in servers:
            srv.should_exit = True
        await task

    # nothing transient survives: no payloads with tokens, no sandboxes
    leftovers = [p for p in cfg.state_dir.rglob("*") if p.name in ("run.env", "mcp.json")]
    assert leftovers == [], leftovers

    # restart: everything is rebuilt from the document store alone
    ctl2 = Controller(cfg)
    ctl2.load()
    assert set(ctl2.changes) == {a.cid, b.cid}
    for old in (a, b):
        new = ctl2.changes[old.cid]
        assert new.change.ship.verdict == "READY"
        assert new.change.model_dump() == old.change.model_dump()      # typed records round-trip
        json.dumps(change_state(ctl2, new))                             # the UI state serialises
        assert list(new.nodes) == list(old.nodes)
        assert {k: n.status for k, n in new.nodes.items()} == {k: n.status for k, n in old.nodes.items()}
        assert {k: r.status for k, r in new.runs.items()} == {k: r.status for k, r in old.runs.items()}
        assert ctl2.docs.list(f"changes/{old.cid}/control/logs/"), "agent logs archived in store"
        assert ctl2.docs.latest_version(f"changes/{old.cid}/control/graph.json")
    assert ctl2.auth.identity.tokens.verify(old_token) is None     # signing key never persisted
    assert all(r.sandbox_ref for _, r in ctl2.all_runs())
    assert not ctl2.docs.list("changes/" + a.cid + "/control/prompts/")


async def _drive(ce):
    n = ce.nodes
    await _wait(ce, lambda: n["H-1"].status == "AWAITING_HUMAN", "H-1")
    assert ce.change.risk.tier == "R2"          # public_api=2 floor
    await ce.human_action("H-1", "accept", "miles")
    assert ce.change.panel == ["reviewer"]

    await _wait(ce, lambda: n["H-2"].status == "AWAITING_HUMAN", "H-2")
    assert n["plan"].cycle == 2                     # critique forced one rework
    assert {"build:T1", "build:T2", "build:T3"} <= set(n)
    assert ce.change.oracle_sha
    with pytest.raises(WorkflowError, match="not a decision owner"):
        await ce.human_action("H-2", "approve", "mallory")
    r = await ce.human_action("H-2", "approve", "miles", {"subjects": ["plan", "REQ-1"]})
    assert sorted(r.pending) == ["REQ-2", "oracle"] and n["H-2"].status == "AWAITING_HUMAN"
    r = await ce.human_action("H-2", "approve", "ana", {"subjects": ["REQ-2", "oracle"]})
    assert r.pending == [] and n["H-2"].status == "ACCEPTED"
    assert ce.change.surface_pins["files"].pins["README.md"]

    await _wait(ce, lambda: n["H-3"].status == "AWAITING_HUMAN", "H-3", 150)
    assert n["build:T1"].cycle == 2                 # review FAIL -> rework
    assert n["build:T2"].cycle == 2                 # verification FAIL -> rework
    t3_runs = [ce.runs[r] for r in n["build:T3"].runs]
    assert any(any(v.startswith("G-B3") for v in vs) for r in t3_runs for vs in r.violations)
    await ce.human_action("H-3", "accept", "miles")

    await _wait(ce, lambda: ce.change.ship is not None, "ship")
    ship = ce.change.ship
    assert ship.verdict == "READY", ship.gates
    assert set(ship.gates) == {
        "approved_delta_spec", "protected_oracle", "task_contract", "deterministic_verification",
        "task_level_independent_review", "integration_or_real_interface_checks",
        "whole_change_acceptance_evidence"}

    # every accepted run's sandbox is gone; documents are namespaced, versioned & immutable
    await _wait(ce, lambda: all(r.sandbox_deleted for r in ce.runs.values()
                                if r.status == "ACCEPTED"), "sandbox cleanup", 20)
    sb_root = ce.cfg.state_dir / "local-sandboxes"
    mine = [p for p in sb_root.glob(f"lg-{ce.cid}-*")] if sb_root.exists() else []
    assert mine == [], mine
    assert ce.docs.latest_version(f"changes/{ce.cid}/plan/spec.md") == 2
    assert ce.docs.latest_version("plan/spec.md") is None
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        ce.docs.conn.execute("UPDATE documents SET content='x'")

    # integration branch contains all work
    head = ce.change.integration_head
    show = lambda p: subprocess.run(["git", "show", f"{head}:{p}"], cwd=ce.git.repo,
                                    capture_output=True, text=True).stdout
    assert show("src/greet.txt") == "hello\n\n"
    assert show("src/bye.txt") == "goodbye\n"
    assert "goodbye" in show("docs/README.md")


async def _r3_workflow(tmp_path):
    _make_repo(tmp_path / "src-repo")
    port = _free_port()
    cfg = _config(tmp_path, port)
    ctl = Controller(cfg)
    servers = await build_servers(ctl, log_level="warning")
    task = asyncio.gather(*(srv.serve() for srv in servers))
    server = servers[0]
    try:
        await _wait(ctl, lambda: server.started, "server start", 20)
        ce = await ctl.start_change("R3: say hello and goodbye")
        n = ce.nodes
        await _wait(ce, lambda: n["H-1"].status == "AWAITING_HUMAN", "H-1")
        await ce.human_action("H-1", "accept", "miles")
        assert ce.change.tier == "R3" and ce.change.panel == ["reviewer", "security", "architecture"]

        await _wait(ce, lambda: n["H-2"].status == "AWAITING_HUMAN", "H-2")
        assert n["baseline"].status == "ACCEPTED"
        r = await ce.human_action("H-2", "approve", "miles")
        assert r.pending == ["independent-baseline"]        # R3: baseline attested before build
        with pytest.raises(Exception, match="G-AT1"):
            await ce.change_action("attest", "miles", {"kind": "independent-baseline",
                                                       "verdict": "pass", "sources": []})
        r = await ce.change_action("attest", "miles", {
            "kind": "independent-baseline", "verdict": "pass", "sources": ["file:baseline/report.md@1"]})
        assert r.warnings == []
        await ce.human_action("H-2", "approve", "miles")
        assert n["H-2"].status == "ACCEPTED"

        await _wait(ce, lambda: n["H-3"].status == "AWAITING_HUMAN", "H-3", 150)
        assert all(f"{b.cycle}:architecture" in b.reviews for b in ce.build_nodes())
        await ce.human_action("H-3", "accept", "miles")
        await _wait(ce, lambda: ce.change.ship is not None, "ship")
        gates = ce.change.ship.gates
        assert ce.change.ship.verdict == "NOT_READY"
        assert gates["security_or_e2e_evaluator"].status == "unevaluable"
        assert gates["rollback_or_forward_fix_evidence"].status == "unevaluable"
        assert gates["independent_baseline"].status == "satisfied"
        assert gates["architecture_or_security_review"].status == "satisfied"
        assert gates["release_observation_plan"].status == "satisfied"

        with pytest.raises(Exception, match="G-AT1"):
            await ce.change_action("attest", "miles", {"kind": "release-observation", "verdict": "pass",
                                                       "sources": ["file:release/plan.md@1"]})
        with pytest.raises(Exception, match="cannot be waived"):
            await ce.change_action("override", "miles", {
                "kind": "waive_gate", "gate": "deterministic_verification",
                "reason": "we really want to skip the checks here", "evidence": "file:release/plan.md@1"})
        sec = ce.latest_att("tasks/T1/reviews/security-cycle-2.md")
        await ce.change_action("attest", "ana", {"kind": "security-evaluation", "verdict": "pass",
                                                 "sources": [sec]})
        await ce.change_action("attest", "ana", {"kind": "rollback-evidence", "verdict": "pass",
                                                 "sources": ["file:release/rollback.md@1"]})
        await ce.change_action("override", "miles", {
            "kind": "add_gate", "gate": "evidence_bundle_or_log",
            "reason": "keep the per-task evidence logs for audit"})
        first = ce.change.ship
        await ce.human_action("ship", "recheck", "miles")
        await _wait(ce, lambda: ce.change.ship is not first, "ship recheck")
        ship = ce.change.ship
        unsatisfied = {k: g for k, g in ship.gates.items() if g.status != "satisfied"}
        assert ship.verdict == "READY", unsatisfied
        assert ship.gates["evidence_bundle_or_log"].added
        assert {"independent_baseline", "security_or_e2e_evaluator",
                "rollback_or_forward_fix_evidence"} <= set(ship.human_judgement_gates)
    finally:
        for srv in servers:
            srv.should_exit = True
        await task


async def _identity_checks(ctl, cfg):
    """The token endpoint and MCP refuse anything but a managed sandbox's valid JWT-SVID."""
    import httpx

    from legion_orch.identity import JWT_SPIFFE
    from legion_orch.workflow.engine import VerifierGrant
    token_url, git = cfg.server.token_url, cfg.server.git_base_url
    operator = f"http://127.0.0.1:{cfg.server.port}"
    ic = cfg.identity
    form = lambda svid, **kw: {"grant_type": "client_credentials", "client_assertion_type": JWT_SPIFFE,
                               "client_assertion": svid, "audience": ic.token_audience,
                               "scope": ic.scope} | kw
    async with httpx.AsyncClient() as c:
        # each listener serves only its own purpose: no git or token endpoint on the operator port,
        # no operator API on the agent-facing ports
        assert (await c.post(f"{operator}/oauth/token")).status_code == 404
        assert (await c.get(f"{operator}/git/app.git/info/refs")).status_code == 404
        for base in (git, token_url):
            assert (await c.get(base.rsplit("/", 1)[0] + "/api/changes")).status_code == 404
        ui = await c.get(f"{operator}/")
        csp = ui.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
        assert "<script>" not in ui.text and " onclick=" not in ui.text
        assert (await c.get(f"{operator}/app.js")).headers["content-type"].startswith("text/javascript")
        unknown = ctl.auth.dev_spire.mint("not-a-managed-sandbox", ic.svid_audience)
        r = await c.post(token_url, data=form(unknown))
        assert r.status_code == 401 and r.json()["error"] == "invalid_client"
        deleted = next(r.sandbox_ref for _, r in ctl.all_runs())
        r = await c.post(token_url, data=form(ctl.auth.dev_spire.mint(deleted, ic.svid_audience)))
        assert r.status_code == 401                               # deleted sandboxes get nothing
        known = "lg-test-verifier"
        ctl.auth.register_verifier(known, VerifierGrant(id="verifier:test", change="c", task="build:T1"))
        r = await c.post(token_url, data=form(ctl.auth.dev_spire.mint(known, "wrong-aud")))
        assert r.status_code == 401
        from legion_orch.identity import DevSpire
        forged = DevSpire(ic.trust_prefix, kid=ctl.auth.dev_spire.kid).mint(known, ic.svid_audience)
        r = await c.post(token_url, data=form(forged))
        assert r.status_code == 401                               # signed by the wrong key
        r = await c.post(token_url, data=form(ctl.auth.dev_spire.mint(known, ic.svid_audience)))
        assert r.status_code == 200 and r.json()["token_type"] == "Bearer"
        tok = r.json()["access_token"]
        # valid identity but no live run (all runs finished) -> MCP refuses, with a visible reason
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}}}
        hdr = {"Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18"}
        for h in ({}, {"Authorization": f"Bearer {tok}"}):
            res = (await c.post(cfg.server.mcp_url, json=body, headers=hdr | h)).json()["result"]
            assert res["isError"] and "no live run" in res["content"][0]["text"]
        auth = {"Authorization": f"Bearer {tok}"}
        r = await c.get(f"{git}/app.git/info/refs?service=git-upload-pack", headers=auth)
        assert r.status_code == 200                               # verifier: read-only fetch
        r = await c.get(f"{git}/app.git/info/refs?service=git-receive-pack", headers=auth)
        assert r.status_code == 403                               # ...but never push
        ctl.auth.unregister_verifier(known)
        r = await c.get(f"{git}/app.git/info/refs?service=git-upload-pack", headers=auth)
        assert r.status_code == 401                               # grant ends with the sandbox

    # task-scoped: a token only works for the live run of the task (and role) it names
    from legion_orch.workflow.model import Run
    ce = next(iter(ctl.changes.values()))
    ce.runs["r9999"] = Run(id="r9999", node_id="build:T1", role="implementer", soul="s", cycle=9,
                           status="ACTIVE", sandbox="lg-x", sandbox_ref="lg-x")
    try:
        good = WorkloadBinding(sandbox="lg-x", change=ce.cid, task="build:T1", role="implementer")
        assert ctl.run_for_bearer(ctl.auth.identity.tokens.issue(good, "x"))[1].id == "r9999"
        for wrong in ({"task": "build:T2"}, {"role": "reviewer"}, {"change": "c-other"},
                      {"sandbox": "lg-y"}):
            bad = dataclasses.replace(good, **wrong)
            assert ctl.run_for_bearer(ctl.auth.identity.tokens.issue(bad, "x")) is None, wrong
        assert ctl.auth.sandbox_binding("lg-x") == good
    finally:
        del ce.runs["r9999"]


def test_operator_api_errors(tmp_path):
    """Writes need the operator token; malformed input is a 400, never a 500."""
    import httpx

    from legion_orch.server.web import build_app
    _make_repo(tmp_path / "src-repo")
    cfg = _config(tmp_path, _free_port())
    cfg.server.operator_token = "op-secret"
    app = build_app(Controller(cfg))          # no lifespan: the controller is not started

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://op") as c:
            assert (await c.get("/api/changes")).json() == []
            r = await c.post("/api/changes", json={"goal": "x"})
            assert r.status_code == 403
            tok = {"X-Operator-Token": "op-secret"}
            r = await c.post("/api/changes", content=b"{not json", headers=tok)
            assert r.status_code == 400 and "JSON" in r.json()["error"]
            r = await c.post("/api/changes", json=["a"], headers=tok)
            assert r.status_code == 400
            r = await c.post("/api/changes/nope/action", json={"action": "attest"}, headers=tok)
            assert r.status_code == 400 and "unknown change" in r.json()["error"]
            assert (await c.get("/api/doc")).status_code == 400
            assert (await c.get("/api/doc?path=nope.md")).status_code == 404
            assert (await c.get("/api/doc?path=x&version=abc")).status_code == 400
    asyncio.run(go())
