import re
import sqlite3
import threading
from pathlib import Path

import pytest

from legion_orch.store import DocStore, DocStoreError, VersionConflict
from legion_orch.workflow import guards, risk
from legion_orch.workflow.assets import AssetError, Assets
from legion_orch.workflow.attest import AttestationError, parse

ROOT = Path(__file__).resolve().parent.parent
SHA = "a" * 40


# ------------------------------------------------------------------ WORKFLOW.md <-> code/assets
DOC = (ROOT / "WORKFLOW.md").read_text()
GUARD_ID_RE = re.compile(r"\|\s*((?:G|H)-[A-Z]+[0-9]+|H-[0-9]+)\s*\|")


def test_workflow_doc_guard_ids_match_code():
    assert set(GUARD_ID_RE.findall(DOC)) == set(guards.GUARDS)


def test_every_guard_is_referenced_outside_the_registry():
    src = "".join(p.read_text() for p in (ROOT / "legion_orch").rglob("*.py")
                  if p.name != "guards.py")
    guards_src = (ROOT / "legion_orch/workflow/guards.py").read_text()
    src += re.sub(r"GUARDS: dict.*?\n}\n", "", guards_src, flags=re.S)
    missing = [g for g in guards.GUARDS if g not in src and not g.startswith("H-")]
    assert not missing, missing


def test_workflow_doc_links_every_asset_and_links_resolve():
    links = set(re.findall(r"\]\((assets/[^)]+)\)", DOC))
    missing = [link for link in links if not (ROOT / link).exists()]
    assert not missing, missing
    files = {str(p.relative_to(ROOT)) for p in (ROOT / "assets").rglob("*") if p.is_file()}
    assert files - links == set(), "assets not documented in WORKFLOW.md"
    a = Assets(ROOT / "assets")
    assert all(f"`{r}`" in DOC for r in a.roles)


def test_assets_folder_validation(tmp_path):
    import shutil
    shutil.copytree(ROOT / "assets", tmp_path / "a")
    Assets(tmp_path / "a")
    (tmp_path / "a/souls/orphan.md").write_text("x")
    with pytest.raises(AssetError, match="not referenced"):
        Assets(tmp_path / "a")
    (tmp_path / "a/souls/orphan.md").unlink()
    (tmp_path / "a/skills/pre-mortem.md").write_text("{{nope}}")
    with pytest.raises(AssetError, match="unknown template variable"):
        Assets(tmp_path / "a")
    (tmp_path / "a/skills/pre-mortem.md").unlink()
    with pytest.raises(AssetError, match="skills/pre-mortem.md not found"):
        Assets(tmp_path / "a")


def test_assets_render_and_pin():
    a = Assets(ROOT / "assets")
    vars_ = {k: "x" for k in ("change_id", "goal", "tier", "repo_name", "base_sha", "head_sha",
                              "branch", "task_id", "task_title", "task_json", "cycle", "role",
                              "context_refs", "feedback", "candidate_sha", "write_scope",
                              "required_docs", "workdir")}
    vars_["task_soul"] = "backend-engineer"
    r = a.render_role("implementer", vars_)
    assert "Backend Engineer" in r.soul and "Harness contract" in r.prompt
    assert r.pin.startswith("sha256:")
    vars_["task_soul"] = "code-reviewer"
    with pytest.raises(AssetError):
        a.render_role("implementer", vars_)
    del vars_["goal"]
    with pytest.raises(AssetError, match="G-D2"):
        a.render_role("intake", vars_)


# ------------------------------------------------------------------ document store
def _store():
    from legion_orch.store import SchemaRegistry
    return DocStore(sqlite3.connect(":memory:"), threading.RLock(), SchemaRegistry(ROOT / "assets/schemas"))


def test_docstore_versions_are_immutable():
    d = _store()
    v1 = d.write("plan/spec.md", "one", author="r1", role="planner")
    assert v1.attestation == "file:plan/spec.md@1"
    with pytest.raises(VersionConflict):
        d.write("plan/spec.md", "two", author="r1", role="planner")            # base missing
    v2 = d.write("plan/spec.md", "two", author="r1", role="planner", base_version=1)
    with pytest.raises(VersionConflict):
        d.write("plan/spec.md", "three", author="r2", role="planner", base_version=1)  # stale
    assert d.read("plan/spec.md", 1).content == "one" and v2.version == 2
    with pytest.raises(sqlite3.DatabaseError):
        d.conn.execute("UPDATE documents SET content='x'")
    with pytest.raises(sqlite3.DatabaseError):
        d.conn.execute("DELETE FROM documents")
    with pytest.raises(DocStoreError):
        d.write("../etc/passwd", "x", author="r", role="r")


# ------------------------------------------------------------------ attestations
def test_attestation_parsing():
    assert parse("file:plan/spec.md@3", ["app"]).version == 3
    a = parse(f"git:org/app/src/x.py@{SHA}", ["org/app", "org"])
    assert (a.repo, a.path) == ("org/app", "src/x.py")
    assert parse(f"git:app@{SHA}", ["app"]).path is None
    for bad in ("file:x", "file:x@0", f"git:other@{SHA}", "git:app@abc", "http://x"):
        with pytest.raises(AttestationError):
            parse(bad, ["app"])


# ------------------------------------------------------------------ risk
def _signals(**scores):
    return {"signals": [{"name": n, "score": scores.get(n, 0), "rationale": "r"} for n in risk.SIGNALS]}


def test_risk_tiers_and_floors():
    assert risk.derive_tier(_signals())["tier"] == "R0"
    assert risk.derive_tier(_signals(ui=1, performance=1, scope_breadth=1))["tier"] == "R1"
    assert risk.derive_tier(_signals(public_api=2))["tier"] == "R2"            # hard floor
    assert risk.derive_tier(_signals(novelty_uncertainty=2))["tier"] == "R0"   # no floor at 2
    assert risk.derive_tier(_signals(security=3))["tier"] == "R3"
    assert risk.validate_signals({"signals": []})
    assert risk.review_panel("R2", {"security": 2}) == ["reviewer", "security"]
    assert not risk.valid_reason("short") and risk.valid_reason("this change deploys nothing at all")


# ------------------------------------------------------------------ plan guards
def _task(tid, files, deps=(), reqs=("REQ-1",)):
    return {"id": tid, "title": tid, "soul": "senior-developer", "requirements": list(reqs),
            "depends_on": list(deps), "files_modified": files, "instructions": "i",
            "acceptance": ["a"], "verification": ["true"]}


ORACLE = {"acceptance_tests": ["tests/acc/test_a.py"], "acceptance_commands": ["pytest tests/acc"],
          "integration_commands": [], "criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "t"}],
          "surfaces": [{"id": "api", "commands": ["pytest tests/acc"], "pins": ["docker-compose.yml"]}]}
SOULS = ["senior-developer", "backend-engineer"]


def check(tasks, reqs=("REQ-1",), tier="R2", oracle=ORACLE):
    from legion_orch.store import SchemaRegistry
    return guards.plan({"tasks": tasks}, oracle, list(reqs), tier, [".github/"], SOULS,
                       SchemaRegistry(ROOT / "assets/schemas"))


def test_plan_guards():
    assert check([_task("T1", ["src/a.py"]), _task("T2", ["src/b.py"], ["T1"])]) == []
    ids = lambda v: {x.split(":")[0] for x in v}
    assert "G-P2" in ids(check([_task("T1", ["a"], ["T2"]), _task("T2", ["b"], ["T1"])]))
    assert "G-P3" in ids(check([_task("T1", ["a"])], reqs=("REQ-1", "REQ-2")))
    assert "G-P6" in ids(check([_task("T1", ["src/"]), _task("T2", ["src/b.py"])]))   # parallel overlap
    assert check([_task("T1", ["src/"]), _task("T2", ["src/b.py"], ["T1"])]) == []    # ordered: fine
    assert "G-P7" in ids(check([_task("T1", ["tests/acc/test_a.py"])]))
    assert "G-P7" in ids(check([_task("T1", [".github/workflows/ci.yml"])]))
    assert "G-P1" in ids(check([_task("T1", ["../x"])]))   # path shape is in the schema
    empty_oracle = {"acceptance_tests": [], "acceptance_commands": [], "integration_commands": [],
                    "criteria": []}
    assert "G-P8" in ids(check([_task("T1", ["a"])], oracle=empty_oracle))
    assert check([_task("T1", ["a"])], tier="R1", oracle=empty_oracle) == []
    assert "G-P1" in ids(check([_task("T1", ["a"])], tier="R1", oracle={}))   # shape: schema
    nosurf = dict(ORACLE, surfaces=[])
    assert "G-P10" in ids(check([_task("T1", ["a"])], oracle=nosurf))
    badsurf = dict(ORACLE, surfaces=[{"id": "API", "commands": [], "pins": ["../x"]}])
    assert len([x for x in check([_task("T1", ["a"])], oracle=badsurf) if x.startswith("G-P1:")]) == 3
    dupsurf = dict(ORACLE, surfaces=[ORACLE["surfaces"][0]] * 2)
    assert "G-P10" in ids(check([_task("T1", ["a"])], oracle=dupsurf))       # cross-item rule
    bad = _task("T1", ["a"])
    bad["soul"] = "code-reviewer"
    assert "G-P5" in ids(check([bad]))


def test_review_and_report_guards():
    att = f"git:app@{SHA}"
    high = {"severity": "BLOCKER", "confidence": "HIGH", "title": "t", "fix": "f", "attestations": [att]}
    kw = dict(pass_word="PASS", fail_word="FAIL", gid="G-RV3", cycle=1, require_checked=True)
    assert guards.verdict({"verdict": "PASS", "findings": [high]}, **kw)[0].startswith("G-RV3")
    assert guards.verdict({"verdict": "FAIL", "findings": []}, **kw)[0].startswith("G-RV3")
    assert guards.verdict({"verdict": "PASS", "findings": []}, **kw)[0].startswith("G-RV4")
    assert guards.verdict({"verdict": "PASS", "findings": [], "checked": ["x"]}, **kw) == []
    assert guards.findings({"findings": [high]}, {att}) == []
    assert guards.findings({"findings": [high]}, set())[0].startswith("G-RV2")
    assert guards.report_shape("PARTIAL", None, None)[0].startswith("G-R1")
    assert guards.report_shape("BLOCKED", "x", "")[0].startswith("G-R2")
    assert guards.scoped_diff(["src/a.py", "tests/acc/t.py"], ["src/"], [], ["tests/acc/"], "G-B3") \
        == ["G-B3: tests/acc/t.py is outside the allowed paths", "G-B3: tests/acc/t.py is a protected path"]
    assert guards.intake("- REQ-1: x\n", _signals(), "- [ ] Q1: which db?", "DONE")[0].startswith("G-I3")


def test_namespacing_and_agent_read_restrictions():
    from legion_orch.server.mcp import _hidden
    from legion_orch.workflow.attest import normalize, qualify
    assert qualify("plan/spec.md", "c1") == "changes/c1/plan/spec.md"
    assert qualify("changes/c2/plan/spec.md", "c1") == "changes/c2/plan/spec.md"
    assert normalize("file:plan/spec.md@2", "c1") == "file:changes/c1/plan/spec.md@2"
    assert normalize(f"git:app@{SHA}", "c1") == f"git:app@{SHA}"
    assert _hidden("changes/c1/control/logs/r0001.log")
    assert not _hidden("changes/c1/control/approvals/spec.json")


def test_agents_cannot_write_other_changes_or_control(tmp_path):
    import types

    from legion_orch.workflow.engine import ChangeEngine, WorkflowError
    from legion_orch.workflow.model import Run
    docs = _store()
    ctl = types.SimpleNamespace(cfg=None, docs=docs, git=None, assets=None, runner=None, lock=None,
                                procs={}, read_tokens={}, version=0)
    ce = ChangeEngine(ctl, "c1")
    run = Run(id="r0001", node_id="plan", role="planner", soul="s", cycle=1,
              write_scope=["changes/c1/plan/"])
    ce.st._events = []
    ce.st.event = lambda *a, **k: 0
    assert ce.docs_write(run, "plan/spec.md", "x", None).path == "changes/c1/plan/spec.md"
    run.write_scope = ["changes/c1/plan/", "changes/c1/tasks/T1/"]   # even a broad scope can't
    for bad in ("changes/c2/plan/spec.md", "control/approvals/x.json", "intake/requirements.md",
                "tasks/T1/verification.json", "tasks/T1/feedback/1.md", "tasks/T1/decisions/1.md"):
        with pytest.raises(WorkflowError, match="G-W1"):
            ce.docs_write(run, bad, "x", None)


def test_review_panels():
    assert risk.review_panel("R3", {}) == ["reviewer", "security", "architecture"]
    assert risk.review_panel("R2", {"scope_breadth": 2}) == ["reviewer", "architecture"]
    assert risk.review_panel("R2", {}) == ["reviewer"]


def test_protected_path_approval_timing_and_coverage():
    import types

    from legion_orch.workflow import governance
    e = types.SimpleNamespace(change=types.SimpleNamespace(protected_approvals=[
        {"task": "T1", "paths": ["tests/acc/test_a.py"], "at": "2026-01-02T00:00:00.000Z"}]))
    assert governance.approved_protected_for(e, "T1", "2026-01-03T00:00:00.000Z") == ["tests/acc/test_a.py"]
    # a run that predates the approval may not touch the path
    assert governance.approved_protected_for(e, "T1", "2026-01-01T00:00:00.000Z") == []
    assert governance.approved_protected_for(e, "T2", "2026-01-03T00:00:00.000Z") == []   # other task
    assert governance.coverage_ok({"covers": None}, {"T1", "T2"})
    assert not governance.coverage_ok({"covers": ["T1"]}, {"T1", "T2"})


def test_store_enforces_bound_schemas():
    import json as _j

    from legion_orch.store import SchemaViolation
    d = _store()
    good = {"tasks": [_task("T1", ["src/a.py"])]}
    v = d.write("changes/c1/plan/taskgraph.json", _j.dumps(good), author="r1", role="planner")
    assert (v.schema, v.schema_sha.startswith("sha256:")) == ("taskgraph", True)
    assert d.read("changes/c1/plan/taskgraph.json").schema == "taskgraph"
    bad = {"tasks": [dict(_task("T1", ["/abs"]), extra=1)]}
    with pytest.raises(SchemaViolation, match="G-W3.*taskgraph"):
        d.write("changes/c1/plan/taskgraph.json", _j.dumps(bad), author="r1", role="planner", base_version=1)
    with pytest.raises(SchemaViolation, match="bound to schema 'taskgraph', not 'json'"):
        d.write("changes/c1/plan/taskgraph.json", _j.dumps(good), author="r1", role="planner",
                base_version=1, schema="json")
    with pytest.raises(SchemaViolation, match="missing heading 'Change'"):
        d.write("changes/c1/plan/specs/REQ-1.md", "# x\n## Current behaviour\n## Acceptance criteria\n",
                author="r1", role="planner")
    run = {"id": "r0001", "node_id": "plan", "role": "planner", "status": "ACTIVE", "cycle": 1, "token": "x"}
    with pytest.raises(SchemaViolation, match="run"):     # run records may never carry a token
        d.write("changes/c1/control/runs/r0001.json", _j.dumps(run), author="controller", role="controller")
    assert d.write("changes/c1/notes/anything.txt", "free text", author="r", role="r").schema == "text"


def test_report_schemas():
    from legion_orch.store import SchemaRegistry
    reg = SchemaRegistry(ROOT / "assets/schemas")
    ok = {"verdict": "PASS", "findings": [], "checked": ["diff"]}
    assert reg.validate_data("report-review", ok) == []
    bad = {"verdict": "OK", "findings": [{"severity": "BAD"}], "checked": []}
    errs = reg.validate_data("report-review", bad)
    assert any("verdict" in e for e in errs) and any("findings/0" in e for e in errs)
    assert reg.validate_data("report-none", {}) == [] and reg.validate_data("report-none", {"x": 1})



def test_svid_verification_and_access_tokens():
    import asyncio
    import time

    from legion_orch.identity import (
        JWT_SPIFFE,
        AccessTokens,
        DevSpire,
        IdentityError,
        IdentityService,
        SvidVerifier,
    )
    from legion_orch.util import bearer
    prefix = "spiffe://openshell.local/openshell/sandbox/"
    spire = DevSpire(prefix)
    v = SvidVerifier(issuer=spire.issuer, audience="legion-controller", trust_prefix=prefix,
                     static_jwks=spire.jwks())
    run = asyncio.run
    assert run(v.verify(spire.mint("sb-1", "legion-controller"))) == "sb-1"
    bad = [spire.mint("sb-1", "other"),                                   # audience
           spire.mint("sb-1", "legion-controller", exp=int(time.time()) - 120),   # expired
           spire.mint("sb-1", "legion-controller", sub="spiffe://evil/x"),        # not a sandbox
           spire.mint("sb-1", "legion-controller", iss="https://other"),          # issuer
           DevSpire(prefix, kid=spire.kid).mint("sb-1", "legion-controller"),     # wrong key
           "not.a.jwt"]
    for svid in bad:
        with pytest.raises(IdentityError):
            run(v.verify(svid))
    svc = IdentityService(v, AccessTokens("legion-controller", 60), "legion")
    form = {"grant_type": "client_credentials", "client_assertion_type": JWT_SPIFFE,
            "client_assertion": spire.mint("sb-1", "legion-controller"), "scope": "legion"}
    binding = {"sandbox": "sb-1", "change": "c1", "task": "build:T1", "role": "implementer"}
    tok = run(svc.token_request(form, lambda ref: binding if ref == "sb-1" else None))["access_token"]
    assert svc.tokens.verify(tok) == binding                       # task-scoped claims
    assert AccessTokens("legion-controller", 60).verify(tok) is None   # other key
    with pytest.raises(IdentityError, match="not managed"):
        run(svc.token_request(form, lambda ref: None))
    with pytest.raises(IdentityError, match="client_credentials"):
        run(svc.token_request(form | {"grant_type": "password"}, lambda ref: binding))
    import base64
    assert bearer("Bearer abc") == "abc"
    assert bearer("Basic " + base64.b64encode(b"x:pw").decode()) == "pw"



def _os_cfg(tmp_path):
    from legion_orch.config import load_config
    (tmp_path / "c.yaml").write_text(f"""
project: p
repo: {{name: app, path: {tmp_path}}}
decision_owners: [miles]
server: {{port: 8765, mcp_port: 8766, git_port: 8767, token_port: 8768}}
identity: {{jwks_uri: "https://spire/keys"}}
sandbox: {{runner: openshell, workspace: team-ml, gateway: prod, image: "reg/agent:1",
          providers: [claude-code], templates_dir: {ROOT / 'policies'}}}
""")
    return load_config(tmp_path / "c.yaml")


def test_template_vocabulary_is_shared_and_documented():
    from legion_orch.sandbox.templates import TEMPLATES, VOCABULARY, placeholders
    used = set()
    for f in TEMPLATES.values():
        used |= placeholders((ROOT / "policies" / f).read_text())
    assert used <= set(VOCABULARY), used - set(VOCABULARY)
    assert set(VOCABULARY) <= used, f"unused vocabulary {set(VOCABULARY) - used}"
    readme = (ROOT / "policies/README.md").read_text()
    assert all(f"`${{{k}}}`" in readme for k in VOCABULARY)
    assert all(f"`{f}`" in readme for f in TEMPLATES.values())


def test_templates_render(tmp_path):
    from legion_orch.sandbox import setup_commands
    from legion_orch.sandbox.runner import OpenShellRunner
    from legion_orch.sandbox.templates import TemplateError, render_text, spec_to_args
    cfg = _os_cfg(tmp_path)
    calls = []

    async def fake_os(*args, timeout=0):
        calls.append(list(args))
        return '{"id": "sb-123"}' if args[:2] == ("sandbox", "get") else ""
    r = OpenShellRunner(cfg)
    r._os = fake_os
    import asyncio

    from legion_orch.sandbox import SandboxSpec
    ref = asyncio.run(r.create(SandboxSpec(name="lg-c1-r0001", payload_dir=tmp_path / "payload",
                                           policy_file=tmp_path / "p.yaml", change="c1",
                                           task="build:T1", role="implementer", run="r0001")))
    assert ref == "sb-123"
    create = calls[0]
    assert create[:2] == ["sandbox", "create"]
    for flag in (["--name", "lg-c1-r0001"], ["--from", "reg/agent:1"], ["--provider", "legion-controller"],
                 ["--provider", "claude-code"], ["--label", "legion.task=build.T1"],
                 ["--upload", f"{tmp_path / 'payload'}:/sandbox"]):
        assert any(create[i:i + 2] == flag for i in range(len(create))), flag
    assert "--detach" in create and "--no-tty" in create
    assert ["--annotation", "legion.task=build:T1"] == create[create.index("--annotation"):][:2]
    import re as _re
    labels = [create[i + 1] for i, a in enumerate(create) if a == "--label"]
    assert len(labels) == 4
    for kv in labels:                         # OpenShell label key/value validation rules
        key, _, val = kv.partition("=")
        assert _re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?", key.split("/")[-1]), kv
        assert _re.fullmatch(r"([A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)?", val), kv
    assert r.cli == ["openshell", "--workspace", "team-ml", "-g", "prod"]
    cmds = setup_commands(cfg, "p.yaml")
    assert cmds[1][5:] == ["provider", "create", "--name", "legion-controller",
                           "--type", "legion-controller", "--runtime-credentials"]
    assert spec_to_args({"a_b": None, "c": False, "d": True, "e": [1, 2], "f": {"k": "v:1"}}) == \
        ["--d", "--e", "1", "--e", "2", "--f", "k=v:1"]
    assert spec_to_args({"label": {"k": ":build:T1/x:"}}) == ["--label", "k=build.T1.x"]
    with pytest.raises(TemplateError, match="whole values"):
        render_text("path: ${MCP_PATH}/x", {"MCP_PATH": "/a"})
    with pytest.raises(TemplateError, match="unknown"):
        render_text("x: ${NOT_A_THING}", {})
    with pytest.raises(TemplateError, match="no value"):
        render_text("x: ${MCP_TOOLS}", {})


def test_generated_policy_and_provider_profile(tmp_path):
    import yaml as _y

    from legion_orch.sandbox import provider_profile, render_policy, setup_commands
    from legion_orch.server import MCP_TOOLS
    cfg = _os_cfg(tmp_path)
    kw = {"mcp_tools": list(MCP_TOOLS)}
    agent = _y.safe_load(render_policy(cfg, agent=True, push=True, **kw))
    mcp = agent["network_policies"]["legion_mcp"]["endpoints"][0]
    git = agent["network_policies"]["legion_git"]["endpoints"][0]
    assert mcp["protocol"] == "mcp" and git["protocol"] == "rest"
    assert (mcp["host"], mcp["port"], mcp["path"]) == ("host.openshell.internal", 8766, "/mcp")
    assert (git["host"], git["port"]) == ("host.openshell.internal", 8767)   # one port per protocol
    calls = [r["allow"] for r in mcp["rules"] if r["allow"]["method"] == "tools/call"]
    assert calls == [{"method": "tools/call", "tool": {"any": list(MCP_TOOLS)}}]
    assert any(r["allow"]["path"].endswith("git-receive-pack") for r in git["rules"])
    judge = _y.safe_load(render_policy(cfg, agent=True, push=False, **kw))
    jgit = judge["network_policies"]["legion_git"]["endpoints"][0]
    assert git["deny_rules"] == []                                       # pushing role
    assert jgit["deny_rules"] == [{"method": "POST", "path": "/git/*.git/git-receive-pack"}]
    ver = _y.safe_load(render_policy(cfg, agent=False, push=False, **kw))
    assert set(ver["network_policies"]) == {"legion_git"}
    prof = provider_profile(cfg, list(MCP_TOOLS))
    token_endpoint = prof["credentials"][0]["token_grant"]["token_endpoint"]
    assert token_endpoint == "http://host.openshell.internal:8768/oauth/token"
    assert all(e["port"] != 8765 for e in prof["endpoints"])          # operator port never exposed
    assert [e["protocol"] for e in prof["endpoints"]] == ["mcp", "rest"]
    assert "Bearer" not in _y.safe_dump(agent) and "token" not in _y.safe_dump(agent).lower()
    # every openshell call targets the configured workspace (and gateway)
    from legion_orch.sandbox.runner import OpenShellRunner
    assert OpenShellRunner(cfg).cli == ["openshell", "--workspace", "team-ml", "-g", "prod"]
    assert all(c[:5] == ["openshell", "--workspace", "team-ml", "-g", "prod"]
               for c in setup_commands(cfg, "p.yaml"))


def test_governance_and_ship_use_only_the_change_view():
    """They depend on the ChangeView protocol, never on engine internals."""
    from legion_orch.workflow.view import ChangeView
    allowed = {n for n in dir(ChangeView) if not n.startswith("__")} | set(ChangeView.__annotations__)
    for f in ("governance.py", "ship.py"):
        used = set(re.findall(r"\be\.([A-Za-z_]+)", (ROOT / "legion_orch/workflow" / f).read_text()))
        assert used <= allowed, f"{f} uses {used - allowed} outside ChangeView"


def test_every_role_has_exactly_one_handler():
    from legion_orch.workflow.roles import REGISTRY
    roles = set(Assets(ROOT / "assets").roles)
    assert set(REGISTRY) == roles, (set(REGISTRY) ^ roles)


def test_role_dispatch_policy():
    """The dispatch policy each handler declares (formerly hard-coded role sets in the engine)."""
    from legion_orch.workflow.roles import roles_where
    panel = {"reviewer", "security", "architecture"}
    assert roles_where("pushes") == {"implementer", "oracle"}
    assert roles_where("panel") == panel
    assert roles_where("judge") == {"critic", "baseline", "integration"} | panel
    assert roles_where("judges_head") == {"baseline", "integration", "release"}
    assert roles_where("reuses_sandbox") == roles_where("needs_fresh_approval") == {"implementer"}
    assert roles_where("sees_draft_plan") == {"critic", "oracle", "baseline"}
    everyone = set(Assets(ROOT / "assets").roles)
    assert everyone - roles_where("sees_requirements") == {"intake"}
    assert everyone - roles_where("sees_oracle_commit") == {"intake", "planner", "critic", "oracle"}


def test_role_handlers_use_only_the_role_context():
    from legion_orch.workflow.view import RoleContext
    allowed = {n for n in dir(RoleContext) if not n.startswith("__")}
    for proto in RoleContext.__mro__:
        allowed |= set(getattr(proto, "__annotations__", {}))
    for f in (ROOT / "legion_orch/workflow/roles").glob("*.py"):
        used = set(re.findall(r"\be\.([A-Za-z_]{2,})", f.read_text()))   # ("e.g." is prose)
        assert used <= allowed, f"{f.name} uses {used - allowed} outside RoleContext"



def test_attestations_cannot_carry_markup():
    for bad in (f"git:app/x'onmouseover=alert(1)@{SHA}", f'git:app/x"y@{SHA}', f"git:app/<b>@{SHA}",
                "file:plan/x'.md@1", "file:plan/a b.md@1"):
        with pytest.raises(AttestationError):
            parse(bad, ["app"])
    assert parse(f"git:app/src/a_b-c.d/e+f=g.py@{SHA}", ["app"]).path == "src/a_b-c.d/e+f=g.py"


def test_ui_has_no_inline_script():
    static = ROOT / "legion_orch/server/static"
    inline = re.compile(r'\son[a-z]+\s*=\s*["\']', re.I)     # HTML attribute form, e.g. onclick="
    html, js = (static / "index.html").read_text(), (static / "app.js").read_text()
    assert "<script>" not in html and not inline.findall(html)
    assert not inline.findall(js), inline.findall(js)          # handlers are data-on + delegation
    assert "localStorage.optoken" not in js


def test_server_ports_must_be_distinct(tmp_path):
    from legion_orch.config import load_config
    (tmp_path / "c.yaml").write_text(f"""
project: p
repo: {{name: app, path: {tmp_path}}}
decision_owners: [miles]
server: {{port: 8765, git_port: 8765}}
sandbox: {{runner: local}}
""")
    with pytest.raises(ValueError, match="distinct"):
        load_config(tmp_path / "c.yaml")



def test_panel_members_cannot_write_each_others_reviews():
    a = Assets(ROOT / "assets")
    v = {"task_id": "T1", "cycle": 1}
    scopes = {r: a.role_paths(r, v)[0] for r in ("reviewer", "security", "architecture")}
    for role, scope in scopes.items():
        for other in scopes:
            path = f"tasks/T1/reviews/{other}-cycle-1.md"
            assert any(path.startswith(w) for w in scope) == (role == other), (role, path)
