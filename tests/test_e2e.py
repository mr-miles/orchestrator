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
from legion_orch.engine import Controller
from legion_orch.server import build_servers

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
    events = "\n".join(f"{e['kind']} {e['node']} {e['message']}" for e in engine.st.events(limit=60)) \
        if hasattr(engine, "st") else ""
    states = {n.id: n.status for n in engine.nodes.values()} if hasattr(engine, "nodes") else {}
    raise AssertionError(f"timeout waiting for {what}; nodes={states}\n{events}")


def _config(tmp_path, port):
    cfg_path = tmp_path / "orchestrator.yaml"
    cfg_path.write_text(textwrap.dedent(f"""
        project: e2e
        repo: {{name: app, path: {tmp_path / 'src-repo'}, base_branch: main}}
        decision_owners: [miles, ana]
        state_dir: state
        assets_dir: {ROOT / 'assets'}
        workflow_doc: {ROOT / 'WORKFLOW.md'}
        server: {{host: 127.0.0.1, port: {port}, mcp_port: {_free_port()}, public_url: "http://127.0.0.1:{port}"}}
        limits: {{max_parallel: 4, review_max_cycles: 3, critique_max_cycles: 2, run_timeout_s: 120}}
        sandbox:
          runner: local
          policy_template: {ROOT / 'policies/agent-policy.template.yaml'}
          verifier_policy_template: {ROOT / 'policies/verifier-policy.template.yaml'}
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
    servers = build_servers(ctl, log_level="warning")
    task = asyncio.gather(*(srv.serve() for srv in servers))
    server = servers[0]
    try:
        await _wait(ctl, lambda: server.started, "server start", 20)
        # two changes share one controller / state_dir and run concurrently
        a = await ctl.start_change("Say hello and goodbye")
        b = await ctl.start_change("Say hello and goodbye, again")
        assert a.cid != b.cid
        await asyncio.gather(_drive(a), _drive(b))
        old_token = ctl.identity.tokens.issue(
            {"sandbox": "s", "change": a.cid, "task": "intake", "role": "intake"}, "x")
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
        assert new.change.ship["verdict"] == "READY"
        assert list(new.nodes) == list(old.nodes)
        assert {k: n.status for k, n in new.nodes.items()} == {k: n.status for k, n in old.nodes.items()}
        assert {k: r.status for k, r in new.runs.items()} == {k: r.status for k, r in old.runs.items()}
        assert len(new.st.events()) == len(old.st.events())
        assert ctl2.docs.list(f"changes/{old.cid}/control/logs/"), "agent logs archived in store"
        assert ctl2.docs.latest_version(f"changes/{old.cid}/control/graph.json")
    assert ctl2.identity.tokens.verify(old_token) is None     # signing key never persisted
    assert all(r.sandbox_ref for _, r in ctl2.all_runs())
    assert not ctl2.docs.list("changes/" + a.cid + "/control/prompts/")


async def _drive(ce):
    n = ce.nodes
    await _wait(ce, lambda: n["H-1"].status == "AWAITING_HUMAN", "H-1")
    assert ce.change.risk["tier"] == "R2"          # public_api=2 floor
    await ce.human_action("H-1", "accept", "miles")
    assert ce.change.panel == ["reviewer"]

    await _wait(ce, lambda: n["H-2"].status == "AWAITING_HUMAN", "H-2")
    assert n["plan"].cycle == 2                     # critique forced one rework
    assert {"build:T1", "build:T2", "build:T3"} <= set(n)
    assert ce.change.oracle_sha
    with pytest.raises(Exception):
        await ce.human_action("H-2", "approve", "mallory")   # not a decision owner
    r = await ce.human_action("H-2", "approve", "miles", {"subjects": ["plan", "REQ-1"]})
    assert sorted(r["pending"]) == ["REQ-2", "oracle"] and n["H-2"].status == "AWAITING_HUMAN"
    r = await ce.human_action("H-2", "approve", "ana", {"subjects": ["REQ-2", "oracle"]})
    assert r["pending"] == [] and n["H-2"].status == "ACCEPTED"
    assert ce.change.surface_pins["files"]["pins"]["README.md"]

    await _wait(ce, lambda: n["H-3"].status == "AWAITING_HUMAN", "H-3", 150)
    assert n["build:T1"].cycle == 2                 # review FAIL -> rework
    assert n["build:T2"].cycle == 2                 # verification FAIL -> rework
    t3_runs = [ce.runs[r] for r in n["build:T3"].runs]
    assert any(any(v.startswith("G-B3") for v in vs) for r in t3_runs for vs in r.violations)
    await ce.human_action("H-3", "accept", "miles")

    await _wait(ce, lambda: ce.change.ship is not None, "ship")
    ship = ce.change.ship
    assert ship["verdict"] == "READY", ship["gates"]
    assert set(ship["gates"]) == {
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
    with pytest.raises(Exception):
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
    servers = build_servers(ctl, log_level="warning")
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
        assert r["pending"] == ["independent-baseline"]        # R3: baseline attested before build
        with pytest.raises(Exception, match="G-AT1"):
            await ce.change_action("attest", "miles", {"kind": "independent-baseline",
                                                       "verdict": "pass", "sources": []})
        r = await ce.change_action("attest", "miles", {
            "kind": "independent-baseline", "verdict": "pass", "sources": ["file:baseline/report.md@1"]})
        assert r["warnings"] == []
        await ce.human_action("H-2", "approve", "miles")
        assert n["H-2"].status == "ACCEPTED"

        await _wait(ce, lambda: n["H-3"].status == "AWAITING_HUMAN", "H-3", 150)
        assert all(f"{b.cycle}:architecture" in b.reviews for b in ce._build_nodes())
        await ce.human_action("H-3", "accept", "miles")
        await _wait(ce, lambda: ce.change.ship is not None, "ship")
        gates = ce.change.ship["gates"]
        assert ce.change.ship["verdict"] == "NOT_READY"
        assert gates["security_or_e2e_evaluator"]["status"] == "unevaluable"
        assert gates["rollback_or_forward_fix_evidence"]["status"] == "unevaluable"
        assert gates["independent_baseline"]["status"] == "satisfied"
        assert gates["architecture_or_security_review"]["status"] == "satisfied"
        assert gates["release_observation_plan"]["status"] == "satisfied"

        with pytest.raises(Exception, match="G-AT1"):
            await ce.change_action("attest", "miles", {"kind": "release-observation", "verdict": "pass",
                                                       "sources": ["file:release/plan.md@1"]})
        with pytest.raises(Exception, match="cannot be waived"):
            await ce.change_action("override", "miles", {
                "kind": "waive_gate", "gate": "deterministic_verification",
                "reason": "we really want to skip the checks here", "evidence": "file:release/plan.md@1"})
        sec = ce._latest_att("tasks/T1/reviews/security-cycle-2.md")
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
        assert ship["verdict"] == "READY", {k: g for k, g in ship["gates"].items() if g["status"] != "satisfied"}
        assert ship["gates"]["evidence_bundle_or_log"]["added"]
        assert {"independent_baseline", "security_or_e2e_evaluator",
                "rollback_or_forward_fix_evidence"} <= set(ship["human_judgement_gates"])
    finally:
        for srv in servers:
            srv.should_exit = True
        await task


async def _identity_checks(ctl, cfg):
    """The token endpoint and MCP refuse anything but a managed sandbox's valid JWT-SVID."""
    import httpx
    from legion_orch.identity import JWT_SPIFFE
    url = cfg.server.public_url
    ic = cfg.identity
    form = lambda svid, **kw: {"grant_type": "client_credentials", "client_assertion_type": JWT_SPIFFE,
                               "client_assertion": svid, "audience": ic.token_audience,
                               "scope": ic.scope} | kw
    async with httpx.AsyncClient() as c:
        unknown = ctl.dev_spire.mint("not-a-managed-sandbox", ic.svid_audience)
        r = await c.post(f"{url}/oauth/token", data=form(unknown))
        assert r.status_code == 401 and r.json()["error"] == "invalid_client"
        deleted = next(r.sandbox_ref for _, r in ctl.all_runs())
        r = await c.post(f"{url}/oauth/token", data=form(ctl.dev_spire.mint(deleted, ic.svid_audience)))
        assert r.status_code == 401                               # deleted sandboxes get nothing
        known = "lg-test-verifier"
        ctl.verifier_refs[known] = {"id": "verifier:test", "change": "c", "task": "build:T1"}
        r = await c.post(f"{url}/oauth/token", data=form(ctl.dev_spire.mint(known, "wrong-aud")))
        assert r.status_code == 401
        from legion_orch.identity import DevSpire
        forged = DevSpire(ic.trust_prefix, kid=ctl.dev_spire.kid).mint(known, ic.svid_audience)
        r = await c.post(f"{url}/oauth/token", data=form(forged))
        assert r.status_code == 401                               # signed by the wrong key
        r = await c.post(f"{url}/oauth/token", data=form(ctl.dev_spire.mint(known, ic.svid_audience)))
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
        r = await c.get(f"{url}/git/app.git/info/refs?service=git-upload-pack", headers=auth)
        assert r.status_code == 200                               # verifier: read-only fetch
        r = await c.get(f"{url}/git/app.git/info/refs?service=git-receive-pack", headers=auth)
        assert r.status_code == 403                               # ...but never push
        del ctl.verifier_refs[known]
        r = await c.get(f"{url}/git/app.git/info/refs?service=git-upload-pack", headers=auth)
        assert r.status_code == 401                               # grant ends with the sandbox

    # task-scoped: a token only works for the live run of the task (and role) it names
    from legion_orch.model import Run
    ce = next(iter(ctl.changes.values()))
    ce.runs["r9999"] = Run(id="r9999", node_id="build:T1", role="implementer", soul="s", cycle=9,
                           status="ACTIVE", sandbox="lg-x", sandbox_ref="lg-x")
    try:
        good = {"sandbox": "lg-x", "change": ce.cid, "task": "build:T1", "role": "implementer"}
        assert ctl.run_for_bearer(ctl.identity.tokens.issue(good, "x"))[1].id == "r9999"
        for wrong in ({"task": "build:T2"}, {"role": "reviewer"}, {"change": "c-other"},
                      {"sandbox": "lg-y"}):
            assert ctl.run_for_bearer(ctl.identity.tokens.issue(good | wrong, "x")) is None, wrong
        assert ctl.sandbox_binding("lg-x") == good
    finally:
        del ce.runs["r9999"]
