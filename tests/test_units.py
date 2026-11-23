import importlib
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from legion_orch.store import DocStore, DocStoreError, VersionConflict
from legion_orch.store.journal import Journal
from legion_orch.store.state_tables import StateTables
from legion_orch.telemetry import Telemetry
from legion_orch.workflow import guards, risk
from legion_orch.workflow.assets import AssetError, Assets
from legion_orch.workflow.attest import AttestationError, parse

ROOT = Path(__file__).resolve().parent.parent
SHA = "a" * 40


# ------------------------------------------------------------------ WORKFLOW.md <-> code/assets
DOC = (ROOT / "WORKFLOW.md").read_text()
GUARD_ID_RE = re.compile(r"\|\s*((?:G|H)-[A-Z]+[0-9]+|H-[0-9]+)\s*\|")



class StubHost:
    """An EngineHost that does nothing: for a ChangeCore on its own."""

    def spawn(self, coro, on_error=None):
        coro.close()

    def changed(self): ...

    def touch(self): ...

    def attention(self, item, raised): ...




    def next_run_id(self):
        return "r0001"

    def new_sandbox_name(self, prefix):
        return f"{prefix}-stub"

def test_the_guards_in_the_workflow_doc_are_the_guards_in_the_code():
    """WORKFLOW.md documents every guard the code enforces, and nothing else (the gates, H-*,
    are the workflow's steps, not code)."""
    src = "".join(p.read_text() for p in (ROOT / "legion_orch").rglob("*.py"))
    doc, code = set(GUARD_ID_RE.findall(DOC)), set(re.findall(r"\bG-[A-Z]+[0-9]+\b", src))
    assert not {g for g in doc - code if not g.startswith("H-")}, "documented, not in the code"
    assert not code - doc, "in the code, not documented"


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
    skill = tmp_path / "a/skills/pre-mortem.md"
    skill.write_text('---\ndescription: "Critique a plan."\n---\n\n{{nope}}')
    with pytest.raises(AssetError, match="unknown template variable"):
        Assets(tmp_path / "a")
    for bad, match in (("no frontmatter", "starts with frontmatter"),
                       ("---\ndescription: x\nallowed-tools: Bash\n---\nbody", "only 'description'"),
                       ("---\ndescription: ''\n---\nbody", "non-empty"),
                       ("---\ndescription: [unclosed\n---\nbody", "invalid frontmatter")):
        skill.write_text(bad)
        with pytest.raises(AssetError, match=match):
            Assets(tmp_path / "a")
    skill.unlink()
    with pytest.raises(AssetError, match="skills/pre-mortem.md not found"):
        Assets(tmp_path / "a")


def test_assets_render_and_pin(tmp_path):
    import shutil

    import yaml
    a = Assets(ROOT / "assets")
    vars_ = {k: "x" for k in ("change_id", "goal", "tier", "repo_name", "base_sha", "head_sha", "oracle_sha",
                              "branch", "task_id", "task_title", "task_json", "cycle", "role",
                              "context_refs", "feedback", "candidate_sha", "write_scope",
                              "required_docs", "workdir", "sandbox_tools", "plan", "step")}
    vars_["task_soul"] = "backend-engineer"
    r = a.render_role("implementer", vars_)
    assert "Backend Engineer" in r.soul and r.pin.startswith("sha256:")
    # the prompt is only the assignment and which skills to load first; every run gets every skill
    assert "Harness contract" not in r.prompt and "`legion-harness-contract`" in r.prompt
    assert "`legion-git-protocol`" in r.prompt
    assert set(r.skills) == {f"legion-{n}" for n in a.skills_}
    md = r.skills["legion-git-protocol"]
    head, body = md.split("\n---\n", 1)
    assert yaml.safe_load(head.removeprefix("---\n")) == {
        "name": "legion-git-protocol", "description": a.skill_descriptions["git-protocol"]}
    assert "branch `x`" in body and "{{" not in md                      # rendered for the run
    # the pin covers every skill, even one the role is not told to load first
    shutil.copytree(ROOT / "assets", tmp_path / "a")
    sec = tmp_path / "a/skills/security-checklist.md"
    sec.write_text(sec.read_text() + "\nOne more check.\n")
    assert Assets(tmp_path / "a").render_role("implementer", vars_).pin != r.pin
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



def test_an_unchanged_write_creates_no_version():
    d = _store()
    d.write("plan/spec.md", "one", author="r1", role="planner")
    same = d.write("plan/spec.md", "one", author="r2", role="planner", base_version=1)
    assert (same.version, same.author) == (1, "r1") and len(d.history("plan/spec.md")) == 1
    with pytest.raises(VersionConflict):                      # concurrency is still checked
        d.write("plan/spec.md", "one", author="r2", role="planner")
    assert d.write("plan/spec.md", "two", author="r2", role="planner", base_version=1).version == 2
    assert d.write("plan/spec.md", "one", author="r2", role="planner", base_version=2).version == 3

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
    assert risk.derive_tier(_signals()).tier == "R0"
    assert risk.derive_tier(_signals(ui=1, performance=1, scope_breadth=1)).tier == "R1"
    assert risk.derive_tier(_signals(public_api=2)).tier == "R2"            # hard floor
    assert risk.derive_tier(_signals(novelty_uncertainty=2)).tier == "R0"   # no floor at 2
    assert risk.derive_tier(_signals(security=3)).tier == "R3"
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
    from legion_orch.workflow.report_data import ReviewData, parse
    high = {"severity": "BLOCKER", "confidence": "HIGH", "title": "t", "fix": "f", "attestations": [att]}
    rd = lambda **d: ReviewData.model_validate(d)
    kw = dict(cycle=1, require_checked=True)
    assert guards.verdict(rd(verdict="PASS", findings=[high]), **kw)[0].startswith("G-RV3")
    assert guards.verdict(rd(verdict="FAIL"), **kw)[0].startswith("G-RV3")
    assert guards.verdict(rd(verdict="PASS"), **kw)[0].startswith("G-RV4")
    assert guards.verdict(rd(verdict="PASS", checked=["x"]), **kw) == []
    found = rd(verdict="FAIL", findings=[high]).findings
    assert guards.findings(found, {att}) == []
    assert guards.findings(found, set())[0].startswith("G-RV2")
    assert guards.findings(found, set(), gid="G-INT1")[0].startswith("G-INT1")
    # every report schema has a typed model, and schema-valid data parses into it
    schemas = {p.stem for p in (ROOT / "assets/schemas").glob("report-*.json")}
    from legion_orch.workflow.report_data import MODEL_FOR_SCHEMA
    assert set(MODEL_FOR_SCHEMA) == schemas
    integ = parse("report-integration", {"verdict": "PASS", "findings": [],
                                         "results": [{"command": "make test", "exit_code": 0}]})
    assert integ.missing(["make test", "make e2e"]) == ["make e2e"] and integ.high == []
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

    from legion_orch.workflow.engine import WorkflowError
    from legion_orch.workflow.engine.base.core import ChangeCore
    from legion_orch.workflow.engine.services.reports import Reports
    from legion_orch.workflow.model import Run
    docs = _store()
    limits = types.SimpleNamespace(max_docs_per_run=3, max_doc_bytes_per_run=10)
    svc = types.SimpleNamespace(cfg=types.SimpleNamespace(limits=limits), docs=docs, git=None,
                                assets=None, runner=None, lock=None, procs={},
                                telemetry=Telemetry.off(),
                                journal=Journal(tmp_path / "journal"),
                                tables=StateTables(sqlite3.connect(":memory:"), threading.RLock()))
    ce = Reports(ChangeCore(svc, StubHost(), "c1"), lifecycle=None, roles=None)  # writes need neither
    run = Run(id="r0001", node_id="plan", role="planner", soul="s", cycle=1,
              write_scope=["changes/c1/plan/"])
    assert ce.docs_write(run, "plan/spec.md", "x", None).path == "changes/c1/plan/spec.md"
    run.write_scope = ["changes/c1/plan/", "changes/c1/tasks/T1/"]   # even a broad scope can't
    for bad in ("changes/c2/plan/spec.md", "control/approvals/x.json", "intake/requirements.md",
                "tasks/T1/verification/cycle-1.json", "tasks/T1/panel/cycle-1.json",
                "tasks/T1/reports/reviewer-cycle-1.json", "control/steps/plan/reports/critic-cycle-1.json",
                "tasks/T1/runs/r0001/assignment.json", "tasks/T1/runs/r0001/outcome.json",
                "tasks/T1/feedback/1.md", "tasks/T1/decisions/1.md"):
        with pytest.raises(WorkflowError, match="G-W1"):
            ce.docs_write(run, bad, "x", None)
    # G-W4: per-run quota (limits: 3 versions, 10 bytes in this test)
    with pytest.raises(WorkflowError, match="G-W4.*bytes"):
        ce.docs_write(run, "plan/big.md", "x" * 10, None)
    ce.docs_write(run, "plan/a.md", "y", None)
    ce.docs_write(run, "plan/b.md", "z", None)
    assert (run.docs_written, run.doc_bytes) == (3, 3)
    with pytest.raises(WorkflowError, match="G-W4.*versions"):
        ce.docs_write(run, "plan/c.md", "w", None)
    # a dry run makes the same checks, and writes and counts nothing
    with pytest.raises(WorkflowError, match="G-W4.*versions"):
        ce.docs_write(run, "plan/c.md", "w", None, dry_run=True)
    run.docs_written = 0
    with pytest.raises(WorkflowError, match="G-W1"):
        ce.docs_write(run, "control/x.json", "{}", None, dry_run=True)
    c = ce.docs_write(run, "plan/c.md", "w", None, dry_run=True)
    assert c.attestation == "file:changes/c1/plan/c.md@1" and docs.latest_version(c.path) is None
    assert (run.docs_written, run.doc_bytes) == (0, 3)


def test_review_panels():
    assert risk.review_panel("R3", {}) == ["reviewer", "security", "architecture"]
    assert risk.review_panel("R2", {"scope_breadth": 2}) == ["reviewer", "architecture"]
    assert risk.review_panel("R2", {}) == ["reviewer"]


def test_protected_path_approval_timing_and_coverage():
    import types

    from legion_orch.workflow import governance
    from legion_orch.workflow.model import ProtectedPathApproval
    e = types.SimpleNamespace(change=types.SimpleNamespace(protected_approvals=[
        ProtectedPathApproval(task="T1", paths=["tests/acc/test_a.py"], approver="miles",
                              reason="r", at="2026-01-02T00:00:00.000Z")]))
    assert governance.approved_protected_for(e, "T1", "2026-01-03T00:00:00.000Z") == ["tests/acc/test_a.py"]
    # a run that predates the approval may not touch the path
    assert governance.approved_protected_for(e, "T1", "2026-01-01T00:00:00.000Z") == []
    assert governance.approved_protected_for(e, "T2", "2026-01-03T00:00:00.000Z") == []   # other task
    assert governance.coverage_ok(types.SimpleNamespace(covers=None), {"T1", "T2"})
    assert not governance.coverage_ok(types.SimpleNamespace(covers=["T1"]), {"T1", "T2"})


def test_docstore_check_makes_every_write_check_and_writes_nothing():
    from legion_orch.store import SchemaViolation
    d = _store()
    c = d.check("changes/c1/plan/spec.md", "one")
    assert (c.attestation, c.unchanged, c.schema) == ("file:changes/c1/plan/spec.md@1", False, "markdown")
    assert d.latest_version("changes/c1/plan/spec.md") is None          # nothing written
    v1 = d.write("changes/c1/plan/spec.md", "one", author="r1", role="planner")
    assert c.sha256 == v1.sha256
    assert d.check("changes/c1/plan/spec.md", "one", base_version=1).unchanged
    assert d.check("changes/c1/plan/spec.md", "two", base_version=1).version == 2
    with pytest.raises(VersionConflict, match="G-W2"):
        d.check("changes/c1/plan/spec.md", "two")                       # base missing
    with pytest.raises(SchemaViolation, match="G-W3.*missing heading 'Change'"):
        d.check("changes/c1/plan/specs/REQ-1.md", "# x\n## Current behaviour\n## Acceptance criteria\n")
    with pytest.raises(DocStoreError, match="invalid document path"):
        d.check("../x.md", "x")
    assert d.latest_version("changes/c1/plan/spec.md") == 1


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
    run = {"role": "planner", "soul": "s", "cycle": 1, "created_at": "2026-11-23T00:00:00.000Z"}
    d.write("changes/c1/tasks/T1/runs/r0001/assignment.json", _j.dumps(run), author="controller",
            role="controller")                                  # a valid assignment
    run["token"] = "x"
    with pytest.raises(SchemaViolation, match="run-assignment.*does not allow"):   # never a token
        d.write("changes/c1/tasks/T1/runs/r0001/assignment.json", _j.dumps(run), author="controller",
                role="controller")
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
    from legion_orch.identity import WorkloadBinding
    binding = WorkloadBinding(sandbox="sb-1", change="c1", task="build:T1", role="implementer")
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
workspaces: [{{name: app, source: {tmp_path}}}]
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
    ref = asyncio.run(r.create(SandboxSpec(name="lg-r0001-0123456789", payload_dir=tmp_path / "payload",
                                           policy_file=tmp_path / "p.yaml", change="c1",
                                           task="build:T1", role="implementer", run="r0001")))
    assert ref == "sb-123"
    create = calls[0]
    assert create[:2] == ["sandbox", "create"]
    for flag in (["--name", "lg-r0001-0123456789"], ["--from", "reg/agent:1"],
                 ["--provider", "legion-mcp"], ["--provider", "legion-a2a"],
                 ["--provider", "legion-git"],
                 ["--provider", "claude-code"],
                 ["--label", "legion.task=build.T1"],
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
    cmds = setup_commands(cfg, "profiles")
    assert [c[5:] for c in cmds] == [
        step for pid in ("legion-mcp", "legion-a2a", "legion-git", "legion-git-push")
        for step in (["profile", "import", "-f", f"profiles/{pid}.yaml"],
                     ["provider", "create", "--name", pid, "--type", pid, "--runtime-credentials"])]
    assert spec_to_args({"a_b": None, "c": False, "d": True, "e": [1, 2], "f": {"k": "v:1"}}) == \
        ["--d", "--e", "1", "--e", "2", "--f", "k=v:1"]
    assert spec_to_args({"label": {"k": ":build:T1/x:"}}) == ["--label", "k=build.T1.x"]
    with pytest.raises(TemplateError, match="whole values"):
        render_text("path: ${MCP_PATH}/x", {"MCP_PATH": "/a"})
    with pytest.raises(TemplateError, match="unknown"):
        render_text("x: ${NOT_A_THING}", {})
    with pytest.raises(TemplateError, match="no value"):
        render_text("x: ${MCP_TOOLS}", {})


def test_generated_policies_and_provider_profiles(tmp_path):
    """Controller access is one provider profile per endpoint, not sandbox policy rules: legion-mcp
    (agent CLI → MCP), legion-a2a (a2a CLI → A2A), legion-git (git, fetch only: receive-pack
    denied) and legion-git-push (git, fetch and push). Sandbox policies have no controller rule."""
    import yaml as _y

    from legion_orch.sandbox import provider_profiles, render_policy, setup_commands
    from legion_orch.server import MCP_TOOLS
    cfg = _os_cfg(tmp_path)
    agent = _y.safe_load(render_policy(cfg, agent=True))
    ver = _y.safe_load(render_policy(cfg, agent=False))
    assert agent["network_policies"] == ver["network_policies"] == {}
    profs = provider_profiles(cfg, list(MCP_TOOLS))
    assert list(profs) == ["legion-mcp", "legion-a2a", "legion-git", "legion-git-push"]
    assert all(p["id"] == pid for pid, p in profs.items())
    [mcp], [a2a], [git], [push] = (profs[p]["endpoints"] for p in profs)
    assert mcp["protocol"] == "mcp" and git["protocol"] == push["protocol"] == a2a["protocol"] == "rest"
    assert (a2a["host"], a2a["port"]) == ("host.openshell.internal", 8769)
    assert {(r["allow"]["method"], r["allow"]["path"]) for r in a2a["rules"]} == \
        {("GET", "/a2a/**"), ("POST", "/a2a/**")}
    assert (mcp["host"], mcp["port"], mcp["path"]) == ("host.openshell.internal", 8766, "/mcp")
    assert (git["host"], git["port"]) == ("host.openshell.internal", 8767)   # one port per protocol
    calls = [r["allow"] for r in mcp["rules"] if r["allow"]["method"] == "tools/call"]
    assert calls == [{"method": "tools/call", "tool": {"any": list(MCP_TOOLS)}}]
    # each endpoint only for the binaries that use it
    assert profs["legion-mcp"]["binaries"] == cfg.sandbox.agent_binaries
    assert profs["legion-a2a"]["binaries"] == cfg.sandbox.a2a_binaries == ["/usr/local/bin/a2a"]
    assert profs["legion-git"]["binaries"] == profs["legion-git-push"]["binaries"] == cfg.sandbox.git_binaries
    # the same allows; deny rules win over every allow, so only legion-git denies push
    assert git["rules"] == push["rules"]
    assert any(r["allow"]["path"].endswith("git-receive-pack") for r in git["rules"])
    assert git["deny_rules"] == [{"method": "POST", "path": "/git/*.git/git-receive-pack"}]
    assert push["deny_rules"] == []
    for p in profs.values():
        grant = p["credentials"][0]["token_grant"]
        # OpenShell's profile lint: token_endpoint is https unless the host is loopback (the
        # gateway makes the exchange host-side)
        assert grant["token_endpoint"] == "http://127.0.0.1:8768/oauth/token"
        assert all(e["port"] != 8765 for e in p["endpoints"])     # operator port never exposed
    # an endpoint's tls is omitted (auto-detect + inspect) or "skip"; skip would disable
    # inspection and the bearer injection, so nothing we render sets it
    def keys(o):
        if isinstance(o, dict):
            return set(o) | {k for v in o.values() for k in keys(v)}
        return {k for v in o for k in keys(v)} if isinstance(o, list) else set()
    assert all("tls" not in keys(doc) for doc in (*profs.values(), agent, ver))
    assert "Bearer" not in _y.safe_dump(agent) and "token" not in _y.safe_dump(agent).lower()
    # every openshell call targets the configured workspace (and gateway)
    from legion_orch.sandbox.runner import OpenShellRunner
    assert OpenShellRunner(cfg).cli == ["openshell", "--workspace", "team-ml", "-g", "prod"]
    assert all(c[:5] == ["openshell", "--workspace", "team-ml", "-g", "prod"]
               for c in setup_commands(cfg, "profiles"))


def test_engine_boundaries():
    """Only the controller knows the Controller; nothing outside the engine package touches an
    engine's private members (saving, state store, helpers)."""
    eng = ROOT / "legion_orch/workflow/engine"
    for f in eng.glob("*.py"):
        if f.name not in ("controller.py", "__init__.py"):
            src = f.read_text()
            assert not re.search(r"from \.controller|\bctl\b", src), f"{f.name} reaches the Controller"
    outside = [p for p in (ROOT / "legion_orch").rglob("*.py") if eng not in p.parents]
    for f in outside + [eng / "controller.py"]:
        bad = re.findall(r"\b(?:ce|engine)\.(?:_\w+|st\b)", f.read_text())
        assert not bad, f"{f.name} uses engine internals {bad}"


def test_the_engine_keeps_its_layers():
    """The import contracts in pyproject.toml (import-linter): the engine's layers import only
    downward (runtime > steps > services > sandboxes > base), nothing outside the engine imports
    more than the engine package's names, the facade never sees the step layer, and the
    workflow core is pure."""
    from importlinter.cli import EXIT_STATUS_SUCCESS, lint_imports
    assert lint_imports(config_filename=str(ROOT / "pyproject.toml"), no_cache=True,
                        no_logo=True) == EXIT_STATUS_SUCCESS     # its output says which broke


def test_the_facade_names_none_of_the_step_layer():
    """change.py fronts the runtime, decisions, reports and logs; the parts composed for the
    step layer (compose.py) are never used through it."""
    src = (ROOT / "legion_orch/workflow/engine/change.py").read_text()
    used = set(re.findall(r"self\._(runner|phases|agents|provisioner|sandboxes)\b", src))
    assert used == set(), used


def test_the_runtime_names_no_phase_or_step_type():
    """The runtime starts, resets, resumes, overrides and retries steps; what a step is made of
    and where it goes on is the step layer's to say, so nothing in runtime/ names a phase's
    status or reads a step type."""
    phases = r"\b(RUNNING|VERIFYING|REVIEWING|MERGING|REWORK)\b"
    types = r"\b(step_for|STEP_TYPES|GATE_TYPES|StepType)\b"
    for f in sorted((ROOT / "legion_orch/workflow/engine/runtime").glob("*.py")):
        src = re.sub(r"(StepStatus|ChangeStatus|NodeStatus)\.\w+", "", f.read_text())
        assert not re.findall(phases, src), f"{f.name} names a phase"
        assert not re.findall(types, src), f"{f.name} reads a step type"


def test_governance_and_ship_use_only_the_change_view():
    """They depend on the ChangeView protocol, never on engine internals."""
    from legion_orch.workflow.view import ChangeView
    allowed = {n for n in dir(ChangeView) if not n.startswith("__")} | set(ChangeView.__annotations__)
    wf = ROOT / "legion_orch/workflow"
    for f in [*sorted((wf / "governance").glob("*.py")), wf / "ship.py"]:
        used = set(re.findall(r"\b(?:self\.)?e\.([A-Za-z_]+)", f.read_text()))
        assert used <= allowed, f"{f.name} uses {used - allowed} outside ChangeView"
        assert "engine" not in f.read_text(), f"{f.name} imports the engine"


def test_every_gate_has_exactly_one_check():
    from legion_orch.workflow import risk
    from legion_orch.workflow.ship import GATE_CHECKS, GateEvaluator
    all_gates = {g for gates in risk.GATES_BY_TIER.values() for g in gates}
    assert set(GATE_CHECKS) == all_gates | set(risk.RELEASE_GATES)     # those: addable only
    assert all(callable(getattr(GateEvaluator, m)) for m in GATE_CHECKS.values())


def test_every_role_has_exactly_one_handler():
    from legion_orch.workflow.roles import REGISTRY
    roles = set(Assets(ROOT / "assets").roles)
    assert set(REGISTRY) == roles, (set(REGISTRY) ^ roles)


def test_role_dispatch_policy():
    """The dispatch policy each handler declares, and where each role's agent works (its step's
    sandbox plan; judges get a fresh one) – formerly hard-coded role sets in the engine."""
    from legion_orch.workflow.engine.sandboxes.plan import Checkout, UntilAccepted, UntilGate
    from legion_orch.workflow.engine.steps.step_types import sandbox_plan
    from legion_orch.workflow.roles import roles_where
    panel = {"reviewer", "security", "architecture"}
    everyone = set(Assets(ROOT / "assets").roles)
    plans = {r: sandbox_plan(r) for r in everyone}
    assert {r for r, p in plans.items() if p.pushes} == {"implementer", "oracle"}
    # the oracle's branch starts from the head at every run, a build task's only at its first
    assert plans["oracle"].base_at_head(True) and plans["oracle"].base_at_head(False)
    assert plans["implementer"].base_at_head(False) and not plans["implementer"].base_at_head(True)
    assert not plans["reviewer"].base_at_head(False)              # judges check out detached
    assert {r for r, p in plans.items() if p.at_head} == {"baseline", "integration", "release",
                                                          "retrospective", "describer"}
    assert {r for r, p in plans.items() if p.checkout == Checkout.CANDIDATE} == panel | {
        "critic", "pr-reviewer"}
    assert {r: p.keep for r, p in plans.items() if p.keeps} == {
        "intake": UntilGate("H-1"), "planner": UntilGate("H-E1"), "oracle": UntilGate("H-2"),
        "implementer": UntilAccepted()}
    assert roles_where("panel") == panel | {"critic", "pr-reviewer"}   # judge a node REVIEWING
    assert roles_where("judge") == {"critic", "baseline", "integration", "pr-reviewer"} | panel
    assert not any(plans[r].keeps for r in roles_where("judge"))   # G-D4
    assert roles_where("needs_fresh_approval") == {"implementer"}
    assert roles_where("sees_draft_plan") == {"critic", "oracle", "baseline"}
    assert everyone - roles_where("sees_requirements") == {"intake"}
    assert everyone - roles_where("sees_oracle_commit") == {"intake", "planner", "critic", "oracle",
                                                            "retrospective"}


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


def test_ui_has_no_inline_script_or_raw_html():
    """The UI renders agent-authored text and is served with a CSP without inline script: its
    page has no inline script or handlers, React never injects raw HTML, and the operator token
    is never persisted beyond the browser session."""
    ui = ROOT / "frontend"
    inline = re.compile(r'\son[a-z]+\s*=\s*["\']', re.I)     # HTML attribute form, e.g. onclick="
    html = (ui / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and not inline.findall(html)
    src = "".join(p.read_text() for p in (ui / "src").rglob("*.ts*"))
    assert "dangerouslySetInnerHTML" not in src and "innerHTML" not in src
    assert "localStorage.setItem(\"optoken" not in src and "localStorage.setItem('optoken" not in src


def test_server_ports_must_be_distinct(tmp_path):
    from legion_orch.config import load_config
    (tmp_path / "c.yaml").write_text(f"""
project: p
workspaces: [{{name: app, source: {tmp_path}}}]
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



def test_plan_limits_and_scale():
    import time

    from legion_orch.store import SchemaRegistry
    reg = SchemaRegistry(ROOT / "assets/schemas")
    too_many = {"tasks": [_task(f"T{i}", [f"src/{i}.py"]) for i in range(1, 102)]}
    assert any("tasks" in e for e in reg.validate_data("taskgraph", too_many))
    # a 100-long dependency chain: waves are iterative (no recursion limit)
    chain = [_task(f"T{i}", [f"src/{i}.py"], [f"T{i - 1}"] if i > 1 else []) for i in range(1, 101)]
    assert guards.waves(chain)["T100"] == 100
    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(80)
    try:
        assert guards.waves(chain)["T100"] == 100
    finally:
        sys.setrecursionlimit(old)
    # 100 independent tasks x 50 files: the parallel-overlap check stays fast
    wide = [_task(f"T{i}", [f"src/t{i}/f{j}.py" for j in range(50)]) for i in range(1, 101)]
    t0 = time.monotonic()
    assert check(wide) == []
    assert time.monotonic() - t0 < 5
    clash = [_task("T1", ["src/"]), _task("T2", ["src/x/y.py"])]
    assert "G-P6" in {x.split(":")[0] for x in check(clash)}
    many_reqs = "\n".join(f"- REQ-{i}: x" for i in range(1, 102))
    assert any("at most 100" in x for x in guards.intake(many_reqs, _signals(), None, "DONE"))


def test_report_limits():
    ok = guards.report_limits("done", [f"git:app@{SHA}"], {"verdict": "PASS"}, None, None)
    assert ok == []
    assert guards.report_limits("x" * 2001, [], {}, None, None)[0].startswith("G-R8: summary")
    assert guards.report_limits("s", ["a"] * 101, {}, None, None)[0].startswith("G-R8: attestations")
    assert guards.report_limits("s", [], {"x": "y" * 300_000}, None, None)[0].startswith("G-R8: data")
    assert guards.report_limits("s", [], {}, "r" * 3000, None)[0].startswith("G-R8: blocked_reason")


def test_transition_rollback_keeps_identity(tmp_path):
    import types

    from legion_orch.workflow.engine.base.core import ChangeCore
    from legion_orch.workflow.model import Change, Node, Run
    svc = types.SimpleNamespace(cfg=None, docs=_store(), hubs={}, assets=None, runner=None,
                                lock=None, procs={}, telemetry=Telemetry.off(),
                                journal=Journal(tmp_path / "journal"),
                                tables=StateTables(sqlite3.connect(":memory:"), threading.RLock()))
    ce = ChangeCore(svc, StubHost(), "c1")
    ce.st.change = Change(id="c1", goal="g", workspace="app", base_sha="a" * 40)
    node = Node(id="plan", kind="plan", title="Plan")
    run = Run(id="r0001", node_id="plan", role="planner", soul="s", cycle=1, status="ACTIVE")
    ce.st.nodes["plan"], ce.st.runs["r0001"] = node, run
    with pytest.raises(RuntimeError), ce.st.transaction():
        run.status, node.status, node.feedback = "REPORTED", "ACCEPTED", [{"x": 1}]
        ce.st.change.tier = "R3"
        ce.st.nodes["build:T1"] = Node(id="build:T1", kind="build", title="T1")  # e.g. new builds
        del ce.st.nodes["plan"]
        raise RuntimeError("transition failed")
    assert ce.nodes["plan"] is node and ce.runs["r0001"] is run       # same objects
    assert (run.status, node.status, node.feedback) == ("ACTIVE", "PENDING", [])
    assert ce.change.tier is None and "build:T1" not in ce.nodes


def test_protected_support_files_are_protected():
    support = dict(ORACLE, protected_support=["tests/acc/conftest.py"])
    touches = check([_task("T1", ["tests/acc/conftest.py"])], oracle=support)
    assert "G-P7" in {x.split(":")[0] for x in touches}
    assert check([_task("T1", ["src/a.py"])], oracle=support) == []
    import types

    from legion_orch.workflow import governance
    from legion_orch.workflow.model import Oracle
    e = types.SimpleNamespace(change=types.SimpleNamespace(oracle=Oracle.model_validate(support),
                                                           setup_pin=None, plan_node="plan"),
                              cfg=types.SimpleNamespace(protected_paths=[".github/"]),
                              latest=lambda _rel: None, current=lambda _base: None)
    assert governance.protected_paths(e) == ["tests/acc/test_a.py", "tests/acc/conftest.py", ".github/"]


def test_cgi_head_parsing():
    from legion_orch.git.http import _parse_cgi_head
    assert _parse_cgi_head(b"Status: 403 Forbidden\r\nContent-Type: text/plain\r\nX-A: 1\r\n\r\nbody") \
        == (403, {"X-A": "1"}, "text/plain", b"body")
    assert _parse_cgi_head(b"Content-Type: application/x-git\n\nPACK") \
        == (200, {}, "application/x-git", b"PACK")
    assert _parse_cgi_head(b"Content-Type: text/plain\r\n") is None        # incomplete header block


def test_every_gate_says_what_approves_it_and_the_runtime_names_no_gate():
    """What approving a gate records is its step type's (its Approval phase's ``approves``);
    the human actions in the runtime hold no table of gates."""
    import inspect

    from legion_orch.workflow.engine.runtime import human
    from legion_orch.workflow.engine.steps import GATE_TYPES
    from legion_orch.workflow.engine.steps.phases import Approval
    from legion_orch.workflow.model import H_CHANGE, H_ENV, H_REQUIREMENTS, H_SPEC
    assert set(GATE_TYPES) == {H_REQUIREMENTS, H_ENV, H_SPEC, H_CHANGE}
    assert {g: sorted(t.phase(Approval).approves.actions) for g, t in GATE_TYPES.items()} == {
        H_REQUIREMENTS: ["accept", "approve"], H_ENV: ["approve"], H_SPEC: ["approve"],
        H_CHANGE: ["accept", "approve"]}
    assert not hasattr(human.HumanGates, "GATE_ACTIONS")
    code = inspect.getsource(human.HumanGates.human_action) + inspect.getsource(
        human.HumanGates._human_gate)
    assert not any(g in code for g in ("H_REQUIREMENTS", "H_ENV", "H_SPEC", "H_CHANGE"))


def test_plan_guard_branches():
    """Each plan rule, failing on its own (the schema's G-P1 is checked separately above)."""
    ids = lambda v: {x.split(":")[0] for x in v}
    msgs = lambda v: " | ".join(v)
    t = _task
    assert "duplicate task ids ['T1']" in msgs(check([t("T1", ["a"]), t("T1", ["b"])]))
    assert "depends on unknown task T9" in msgs(check([t("T1", ["a"], ["T9"])]))
    assert "cites unknown requirement REQ-9" in msgs(check([t("T1", ["a"], reqs=("REQ-1", "REQ-9"))]))
    blank = t("T1", ["a"]) | {"verification": ["  "]}       # schema-valid, but no real command
    assert {"G-P4"} == ids(check([blank]))
    # path shape is the schema's (G-P1); paths_valid re-checks it in case the schema loosens
    plan_guards = importlib.import_module("legion_orch.workflow.guards.plan")  # (shadowed by plan())
    assert plan_guards.paths_valid([t("T1", []) | {"files_forbidden": ["/etc/passwd", "a/../b"]}]) == [
        "G-P6: task T1 has empty files_modified", "G-P6: task T1 has invalid path '/etc/passwd'",
        "G-P6: task T1 has invalid path 'a/../b'"]
    crit = dict(ORACLE, criteria=[{"id": "AC-1", "requirement": "REQ-7", "text": "t"}])
    assert "must reference an accepted requirement" in msgs(check([t("T1", ["a"])], oracle=crit))
    # parallel overlap: exact/exact, pattern/exact, exact/pattern, glob/glob
    for a, b in ((["x.py"], ["x.py"]), (["src/"], ["src/a.py"]), (["src/a.py"], ["src/"]),
                 (["src/*.py"], ["src/*.py"])):
        assert "G-P6" in ids(check([t("T1", a), t("T2", b)])), (a, b)
    assert check([t("T1", ["src/a.py"]), t("T2", ["docs/"])]) == []
    # waves: 1 + deepest dependency, on a long chain without recursion
    chain = [t(f"T{i}", [f"f{i}"], [f"T{i - 1}"] if i else []) for i in range(3000)]
    assert guards.waves(chain)["T2999"] == 3000


def test_report_and_work_guard_branches(tmp_path):
    from legion_orch.workflow import attest
    from legion_orch.workflow.assets import GitMode
    docs = _store()
    v1 = docs.write("changes/c1/plan/a.md", "one", author="r0001", role="planner")
    docs.write("changes/c1/plan/a.md", "two", author="r0001", role="planner", base_version=1)
    docs.write("changes/c1/plan/b.md", "x", author="r0002", role="planner")
    parse = lambda *refs: [attest.parse(r, ["app"]) for r in refs]
    _, v = guards.required_docs("r0001", ["changes/c1/plan/a.md", "changes/c1/plan/b.md"],
                                parse(v1.attestation, "file:changes/c1/plan/b.md@1"), docs)
    assert "is not the latest version (latest 2)" in v[0] and "was not written by this run" in v[1]
    sha, v = guards.required_git(GitMode.COMMIT, parse(f"git:app@{SHA}", f"git:app@{'b' * 40}"), "app", None)
    assert sha is None and v[0].startswith("G-R5: attest exactly one commit")
    sha, v = guards.required_git(GitMode.CANDIDATE, parse(f"git:app@{'b' * 40}"), "app", SHA)
    assert v[0].startswith("G-RV1")
    assert guards.required_git(GitMode.HEAD, parse(f"git:app@{SHA}"), "app", SHA) == (SHA, [])
    assert guards.load_json(docs, None, "G-X")[1] == ["G-X: document not attested"]
    assert "not valid JSON" in guards.load_json(docs, parse(v1.attestation)[0], "G-X")[1][0]
    assert guards.scoped_diff([], ["src/"], [], [], "G-B3") == ["G-B3: commit changes no files"]
    assert guards.scoped_diff(["src/k.pem"], ["src/"], ["*.pem"], [], "G-B3") == \
        ["G-B3: src/k.pem is forbidden for this task"]
    assert len(guards.release_docs("# plan\n", "revert", SHA)) == 3      # + no strategy line
    ok_plan = "## Health criteria\n## Rollback criteria\n"
    assert guards.release_docs(ok_plan, f"Strategy: rollback\ngit revert to {SHA}", SHA) == []
    assert guards.intake("- REQ-1: a\n- REQ-1: b\n", _signals(), None, "DONE")[0] == \
        "G-I1: duplicate requirement ids ['REQ-1']"
    many = "".join(f"- REQ-{i}: r\n" for i in range(101))
    assert "at most 100 per change" in guards.intake(many, _signals(), None, "DONE")[0]
    assert "G-I1" in guards.intake("no ids here", _signals(), None, "DONE")[0]


def test_a_workspace_source_is_optional_and_relative_to_the_config(tmp_path):
    from legion_orch.config import load_config
    for ws in ("{name: app}", "{name: app, source: ''}", "{name: app, source: '  '}"):
        (tmp_path / "c.yaml").write_text(f"project: p\nworkspaces: [{ws}]\ndecision_owners: [m]\n"
                                         "sandbox: {runner: local}\n")
        w = load_config(tmp_path / "c.yaml").workspaces[0]
        assert w.source is None and w.base_branch == "main" and w.publish_prefix == "agent/", ws
    (tmp_path / "c.yaml").write_text("project: p\nworkspaces: [{name: app, source: src}]\n"
                                     "decision_owners: [m]\nsandbox: {runner: local}\n")
    assert load_config(tmp_path / "c.yaml").workspace("app").source == str(tmp_path / "src")


def test_workspaces_are_checked_at_load(tmp_path):
    import pytest

    from legion_orch.config import load_config

    def load(ws: str):
        (tmp_path / "c.yaml").write_text(f"project: p\n{ws}\ndecision_owners: [m]\n"
                                         "sandbox: {runner: local}\n")
        return load_config(tmp_path / "c.yaml")
    for ws, msg in [("repo: {name: app}", "replaced by workspaces"),
                    ("workspaces: []", "at least one"),
                    ("workspaces: [{name: a}, {name: a}]", "unique"),
                    ("workspaces: [{name: ../x}]", "path segment"),
                    ("workspaces: [{name: x.git}]", "path segment"),
                    ("workspaces: [{name: a, publish_prefix: legion/}]", "outside legion/"),
                    ("workspaces: [{name: a, publish_prefix: 'bad..'}]", "valid branch name"),
                    ("workspaces: [{name: a, path: x}]", "unknown WorkspaceConfig keys")]:
        with pytest.raises(ValueError, match=msg):
            load(ws)
    cfg = load("workspaces: [{name: a}, {name: b, publish_prefix: bots/legion-}]")
    assert [w.name for w in cfg.workspaces] == ["a", "b"]
    assert cfg.workspace("b").publish_prefix == "bots/legion-"
    with pytest.raises(KeyError, match="no workspace 'c'"):
        cfg.workspace("c")


def test_sandbox_names_are_first_names_and_fit_the_gateway_limit():
    """lg-<name> / lgv-<name>; OpenShell: a DNS label of at most 19 characters without '--'
    (NVIDIA/OpenShell#2651). Every name fits with the longer prefix and a 4-digit suffix."""
    import re as _re

    from legion_orch.sandbox import AGENT_PREFIX, MAX_NAME, VERIFIER_PREFIX, check_name, human_name
    from legion_orch.sandbox.names import NAMES
    assert len(VERIFIER_PREFIX) + 1 + max(map(len, NAMES)) + len("-9999") <= MAX_NAME
    assert len(set(NAMES)) == len(NAMES) and all(_re.fullmatch(r"[a-z]+", w) for w in NAMES)
    for prefix in (AGENT_PREFIX, VERIFIER_PREFIX):
        n = human_name(prefix, lambda _: False)
        assert n.removeprefix(prefix + "-") in NAMES and check_name(n) == n
    for bad in ("lg-c261114-a16e-r0001", "lg--x", "Lg-x", "-lg", "lg-x-", "lg_x"):
        with pytest.raises(ValueError, match="at most 19"):
            check_name(bad)


def test_names_are_not_reused_and_get_numbers_once_all_are_used():
    from legion_orch.sandbox import human_name
    from legion_orch.sandbox.names import NAMES
    taken = {f"lg-{n}" for n in NAMES} - {"lg-ada"}
    assert human_name("lg", taken.__contains__) == "lg-ada"          # the one left
    taken.add("lg-ada")
    second = human_name("lg", taken.__contains__)
    assert second.endswith("-2") and second.removeprefix("lg-").removesuffix("-2") in NAMES
    taken |= {f"lg-{n}-2" for n in NAMES}
    assert human_name("lg", taken.__contains__).endswith("-3")


@pytest.mark.parametrize("exists", [False, True])
def test_setup_workspace_imports_or_updates(tmp_path, exists):
    """A fresh workspace imports each profile and creates its provider; a configured one updates
    each profile (with its resource_version) and keeps the providers."""
    import subprocess

    import yaml as _y

    from legion_orch.sandbox import setup_workspace
    cfg = _os_cfg(tmp_path)
    calls = []

    def run(cmd, probe):
        calls.append((cmd[5:], probe))
        if cmd[5:7] == ["profile", "export"]:
            out = f'{{"id": "{cmd[7]}", "resource_version": "42"}}' if exists else ""
            return subprocess.CompletedProcess(cmd, 0 if exists else 1, out, "")
        if cmd[5:7] == ["provider", "get"]:
            return subprocess.CompletedProcess(cmd, 0 if exists else 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    out = tmp_path / "profiles"
    ids = ["legion-mcp", "legion-git", "legion-git-push"]
    assert setup_workspace(cfg, {p: {"id": p} for p in ids}, out, run, say=lambda _: None) == 0
    steps = [c for c, probe in calls if not probe]
    written = {p: _y.safe_load((out / f"{p}.yaml").read_text()) for p in ids}
    if exists:
        assert steps == [["profile", "update", "-f", str(out / f"{p}.yaml"), p] for p in ids]
        assert written == {p: {"id": p, "resource_version": "42"} for p in ids}
    else:
        assert [s[:2] for s in steps] == [["profile", "import"], ["provider", "create"]] * 3
        assert [s[3] for s in steps if s[:2] == ["provider", "create"]] == ids
        assert written == {p: {"id": p} for p in ids}


def test_default_agent_command_finds_the_payload(tmp_path):
    """run-agent.sh exports the payload location, so the default command works under any runner
    (here a payload that is not at /sandbox) – with a stub `claude` on PATH."""
    import os
    import shlex
    import shutil
    import subprocess

    from legion_orch.config import SandboxConfig, agent_command
    from legion_orch.sandbox import RUN_AGENT_SH
    sb, bin_dir = tmp_path / "sb", tmp_path / "bin"
    (sb / "payload").mkdir(parents=True)
    bin_dir.mkdir()
    for name, text in (("soul.md", "SOUL"), ("prompt.md", "PROMPT"), ("mcp.json", "{}")):
        (sb / "payload" / name).write_text(text)
    (sb / "payload/skills/legion-new").mkdir(parents=True)
    (sb / "payload/skills/legion-new/SKILL.md").write_text("new")
    cc = tmp_path / "claude-config"
    for old in ("legion-old", "mine"):                   # an earlier run's, and the image's own
        (cc / "skills" / old).mkdir(parents=True)
        (cc / "skills" / old / "SKILL.md").write_text(old)
    assert agent_command(SandboxConfig()).startswith("claude -p ")
    assert '--tools Skill ' in agent_command(SandboxConfig(), tools=False)
    assert "--tools" not in agent_command(SandboxConfig())
    # an absolute agent_bin, as `bash -lc` may reset PATH
    cmd = agent_command(SandboxConfig(agent_bin=str(bin_dir / "claude")))
    (sb / "payload" / "run.env").write_text(f"LEGION_NEEDS_REPO=0\nLEGION_AGENT_COMMAND={shlex.quote(cmd)}\n")
    (sb / "payload" / "run-agent.sh").write_text(RUN_AGENT_SH)
    (bin_dir / "claude").write_text('#!/bin/sh\nprintf "%s\\n" "$@"; echo "stdin=$(cat)"\n')
    (bin_dir / "claude").chmod(0o755)
    env = {**os.environ, "SANDBOX_ROOT": str(sb), "CLAUDE_CONFIG_DIR": str(cc)}
    out = subprocess.run(["bash", str(sb / "payload" / "run-agent.sh")], env=env, check=True,
                         capture_output=True, text=True).stdout.splitlines()
    assert sorted(p.name for p in (cc / "skills").iterdir()) == ["legion-new", "mine"]
    shutil.rmtree(sb / "payload/skills")                 # a reflection's payload brings none
    subprocess.run(["bash", str(sb / "payload" / "run-agent.sh")], env=env, check=True,
                   capture_output=True)
    assert sorted(p.name for p in (cc / "skills").iterdir()) == ["legion-new", "mine"]
    assert out[out.index("--mcp-config") + 1] == str(sb / "payload" / "mcp.json")
    assert out[out.index("--append-system-prompt") + 1] == "SOUL"
    assert out[-1] == "stdin=PROMPT"
    assert "--resume" not in out                         # no session to continue


def test_the_agent_command_resumes_a_session_and_cannot_be_replaced(tmp_path):
    from legion_orch.config import SandboxConfig, agent_command, load_config
    cmd = agent_command(SandboxConfig(agent_bin="/opt/my claude"))
    assert cmd.startswith("'/opt/my claude' -p ")
    assert '${LEGION_RESUME_SESSION:+--resume "$LEGION_RESUME_SESSION"}' in cmd
    cfg = tmp_path / "orchestrator.yaml"
    cfg.write_text("project: p\nworkspaces: [{name: app, source: ., base_branch: main}]\n"
                   "decision_owners: [miles]\nsandbox: {agent_command: claude -p}\n")
    with pytest.raises(ValueError, match="agent_command was removed.*agent_bin"):
        load_config(cfg)


def test_sandbox_root_and_user_follow_the_image(tmp_path):
    """An image without /sandbox: payload, working copy, policy paths and the run-as user all come
    from sandbox.root / run_as_user / run_as_group."""
    import asyncio

    import yaml as _y

    from legion_orch.sandbox import LocalRunner, OpenShellRunner, SandboxSpec, render_policy
    cfg = _os_cfg(tmp_path)
    cfg.sandbox.root, cfg.sandbox.run_as_user, cfg.sandbox.run_as_group = "/home/agent", "agent", "agent"
    for agent in (True, False):
        pol = _y.safe_load(render_policy(cfg, agent=agent))
        assert pol["filesystem_policy"]["read_write"] == ["/home/agent", "/tmp", "/dev/null"]
        assert (pol["process"]["run_as_user"], pol["process"]["run_as_group"]) == ("agent", "agent")
    calls = []

    async def fake_os(*args, timeout=0):
        calls.append(list(args))
        return '{"id": "sb-1"}' if args[:2] == ("sandbox", "get") else ""
    r = OpenShellRunner(cfg)
    r._os = fake_os
    asyncio.run(r.create(SandboxSpec(name="lg-r0001-0123456789", payload_dir=tmp_path / "payload",
                                     policy_file=tmp_path / "p.yaml", change="c1", task="t",
                                     role="implementer", run="r0001")))
    create = calls[0]
    assert create[create.index("--upload") + 1] == f"{tmp_path / 'payload'}:/home/agent"
    local = LocalRunner(cfg, gateway=None)
    assert local._map("sb", "/home/agent/work") == str(local._root("sb") / "work")
    assert local._map("sb", "/home/agentx") == "/home/agentx"          # only the root itself


def test_local_sandboxes_get_their_own_claude_config_dir(tmp_path, monkeypatch):
    """The local runner's agent runs as the operator: it gets a Claude config dir of its own (the
    skills and sessions go there, never into the operator's ~/.claude), with the operator's
    credentials linked in."""
    import asyncio

    from legion_orch.sandbox import LocalRunner, SandboxSpec
    own = tmp_path / "operator-claude"
    own.mkdir()
    (own / ".credentials.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(own))
    cfg = _os_cfg(tmp_path)

    async def gateway(name):
        return "tok"
    local = LocalRunner(cfg, gateway=gateway)
    (tmp_path / "payload").mkdir()
    asyncio.run(local.create(SandboxSpec(name="lg-r0001-0123456789", payload_dir=tmp_path / "payload",
                                         policy_file=tmp_path / "p.yaml", change="c1", task="t",
                                         role="implementer", run="r0001")))
    d = local._root("lg-r0001-0123456789") / "claude"
    assert (d / ".credentials.json").resolve() == own / ".credentials.json"
    env = asyncio.run(local._env("lg-r0001-0123456789"))
    assert env["CLAUDE_CONFIG_DIR"] == str(d) and not (own / "skills").exists()


def test_mcp_policy_allows_every_core_method_the_server_answers(tmp_path):
    """The gateway refuses MCP methods the policy does not list. Every method our server answers
    must be allowed in the legion-mcp provider profile, and nothing else.
    (subscriptions/listen was once missing and denied.) Prompts and resources are not offered:
    the tools_only middleware refuses them although the SDK registers handlers."""
    from mcp.server.mcpserver import MCPServer

    from legion_orch.sandbox import provider_profiles
    from legion_orch.sandbox.templates import MCP_VERSIONS
    from legion_orch.server import MCP_TOOLS
    from legion_orch.server.mcp import NOT_OFFERED
    cfg = _os_cfg(tmp_path)
    endpoints = provider_profiles(cfg, list(MCP_TOOLS))["legion-mcp"]["endpoints"]
    handlers = set(MCPServer("probe")._lowlevel_server._request_handlers)
    served = {m for m in handlers if m.split("/", 1)[0] not in NOT_OFFERED}
    lifecycle = {"initialize", "notifications/initialized", "notifications/cancelled"}
    for ep in endpoints:
        allowed = {r["allow"]["method"] for r in ep["rules"]}
        assert allowed == served | lifecycle, sorted(served ^ (allowed - lifecycle))
        assert ep["mcp"]["versions"] == MCP_VERSIONS


def test_gateway_inspects_mcp_requests_as_large_as_the_largest_document(tmp_path):
    """OpenShell drops MCP requests over mcp.max_body_bytes (default 64 KiB) with no response, which
    agents see as connection resets on docs_write. The legion-mcp profile renders the configured
    limit, and a docs_write of the largest document the store accepts – JSON content, so heavily
    escaped – fits in it."""
    import json as _j

    from legion_orch.sandbox import provider_profiles
    from legion_orch.server import MCP_TOOLS
    from legion_orch.store.docstore import MAX_DOC_BYTES
    cfg = _os_cfg(tmp_path)
    [prof_ep] = provider_profiles(cfg, list(MCP_TOOLS))["legion-mcp"]["endpoints"]
    limit = cfg.server.mcp_max_body_bytes
    # a profile endpoint spells it json_rpc_max_body_bytes (its mcp block may not carry it)
    assert prof_ep["json_rpc_max_body_bytes"] == limit
    assert set(prof_ep["mcp"]) <= {"versions", "strict_tool_names", "allow_all_known_mcp_methods"}
    row = {"id": "REQ-1", "command": 'pytest -k "greet and not slow"', "path": "src\\greet.txt"}
    doc = _j.dumps({"tests": [row] * (MAX_DOC_BYTES // len(_j.dumps(row)))})[:MAX_DOC_BYTES]
    body = _j.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "docs_write", "arguments": {"path": "plan/oracle.json", "content": doc}}})
    from legion_orch.server.web import mcp_request_limit
    served = mcp_request_limit(cfg)
    assert len(body.encode()) < served < limit, (len(body), served, limit)


def test_sandbox_exec_gets_no_stdin(tmp_path, monkeypatch):
    """`sandbox exec` forwards its stdin into the sandbox, where nothing reads it: input that fills
    the supervisor's buffer gets the command killed (exit 74), so the controller's stdin is never
    passed on."""
    import asyncio

    from legion_orch.sandbox import runner as rmod
    seen = []

    class Out:
        async def read(self, n):
            return b""

    class Proc:
        returncode = 0
        stdout = Out()

        async def wait(self):
            return 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kw):
        seen.append((argv, kw.get("stdin")))
        return Proc()
    monkeypatch.setattr(rmod.asyncio, "create_subprocess_exec", fake_exec)
    r = rmod.OpenShellRunner(_os_cfg(tmp_path))
    asyncio.run(r.exec("sb", ["true"]))

    async def agent():
        await (await r.start_agent("sb", tmp_path / "agent.log")).wait()
    asyncio.run(agent())
    assert [a[a.index("sandbox") + 1] for a, _ in seen] == ["exec", "exec"]
    assert all(stdin == asyncio.subprocess.DEVNULL for _, stdin in seen)


def test_a_cancelled_command_does_not_leave_its_process_running(tmp_path):
    """Cancelling a task (as the controller does at shutdown) kills and reaps the command it was
    waiting for, instead of leaving it running past the event loop."""
    import asyncio
    import os

    from legion_orch.git.http import git_exec_path
    from legion_orch.sandbox.runner import _run
    pidfile = tmp_path / "pid"

    async def go():
        task = asyncio.create_task(_run(["sh", "-c", f"echo $$ > {pidfile}; exec sleep 30"], 60))
        while not pidfile.exists() or not pidfile.read_text().strip():
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    with pytest.raises(ProcessLookupError):        # killed and reaped, not a zombie
        os.kill(int(pidfile.read_text()), 0)
    assert git_exec_path() and os.path.isdir(git_exec_path())


def test_the_checkout_steps_clone_then_detach_or_branch(tmp_path):
    """The controller's preparation steps, as the real scripts: clone.sh (a partial clone), then
    checkout.sh (detached: no branch, no identity; older contents fetched on demand) for a role
    that does not push, or branch.sh (the run's branch at a commit, with an identity) for one
    that does; run-agent.sh then starts the agent in the checkout."""
    import os
    import shlex
    import subprocess

    from legion_orch.sandbox import payload_scripts
    g = lambda *a, cwd=tmp_path / "src": subprocess.run(["git", *a], cwd=cwd, check=True,
                                                        capture_output=True, text=True).stdout.strip()
    (tmp_path / "src").mkdir()
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (tmp_path / "src" / "a.txt").write_text("one\n")
    g("add", "-A")
    g("commit", "-q", "-m", "one")
    base = g("rev-parse", "HEAD")
    (tmp_path / "src" / "a.txt").write_text("two\n")
    g("commit", "-q", "-am", "two")
    cand = g("rev-parse", "HEAD")
    hub = tmp_path / "hub.git"
    g("clone", "-q", "--bare", str(tmp_path / "src"), str(hub), cwd=tmp_path)
    g("config", "uploadpack.allowFilter", "true", cwd=hub)
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}

    def sandbox(name: str, agent: str):
        sb = tmp_path / name
        (sb / "payload").mkdir(parents=True)
        for script, text in payload_scripts().items():
            (sb / "payload" / script).write_text(text)
        vals = {"LEGION_ROLE": "implementer", "LEGION_RUN_ID": "r1",
                "LEGION_GIT_URL": f"file://{hub}", "LEGION_AGENT_COMMAND": agent}
        (sb / "payload" / "run.env").write_text("".join(f"{k}={shlex.quote(v)}\n"
                                                        for k, v in vals.items()))
        return lambda script, *args: subprocess.run(
            ["bash", str(sb / "payload" / script), *args], check=True, capture_output=True,
            text=True, env=env).stdout.splitlines()

    step = sandbox("judge", f"git rev-parse HEAD; git branch --show-current; echo branch-done; "
                            f"git config user.name || echo no-identity; git diff --stat {base} "
                            f"{cand}; git config remote.origin.promisor")
    step("clone.sh")
    step("checkout.sh", cand)
    out = step("run-agent.sh")
    i = out.index(cand)
    assert out[i + 1] == "branch-done"                  # no branch: detached
    assert "no-identity" in out                         # nothing set up to commit with
    assert any("a.txt | 2 +-" in x for x in out)        # the older commit's contents, on demand
    assert out[-1] == "true"                            # a partial (promisor) clone

    step = sandbox("builder", "git branch --show-current; git rev-parse HEAD; git config user.name")
    step("clone.sh")
    step("branch.sh", "legion/c1/build-t1", base)
    assert step("run-agent.sh")[-3:] == ["legion/c1/build-t1", base, "legion-implementer"]


def test_the_stored_node_schema_knows_every_node_status():
    """A status the schema does not list fails every save of a node in it (G-W3) – in a
    background task, where only the log shows it."""
    import json

    from legion_orch.workflow.model import NodeStatus
    schema = json.loads((Path(__file__).parent.parent / "assets/schemas/node.json").read_text())
    assert set(schema["oneOf"][0]["properties"]["status"]["enum"]) == {s.value for s in NodeStatus}
