import re
import sqlite3
import threading
from pathlib import Path

import pytest

from legion_orch import guards, risk
from legion_orch.assets import AssetError, Assets
from legion_orch.attest import AttestationError, parse
from legion_orch.docstore import DocStore, DocStoreError, VersionConflict

ROOT = Path(__file__).resolve().parent.parent
SHA = "a" * 40


# ------------------------------------------------------------------ WORKFLOW.md <-> code/assets
DOC = (ROOT / "WORKFLOW.md").read_text()
GUARD_ID_RE = re.compile(r"\|\s*((?:G|H)-[A-Z]+[0-9]+|H-[0-9]+)\s*\|")


def test_workflow_doc_guard_ids_match_code():
    assert set(GUARD_ID_RE.findall(DOC)) == set(guards.GUARDS)


def test_every_guard_is_referenced_outside_the_registry():
    src = "".join(p.read_text() for p in (ROOT / "legion_orch").glob("*.py")
                  if p.name != "guards.py")
    src += re.sub(r"GUARDS: dict.*?\n}\n", "", (ROOT / "legion_orch/guards.py").read_text(), flags=re.S)
    missing = [g for g in guards.GUARDS if g not in src and not g.startswith("H-")]
    assert not missing, missing


def test_workflow_doc_links_every_asset_and_links_resolve():
    links = set(re.findall(r"\]\((assets/[^)]+)\)", DOC))
    assert all((ROOT / l).exists() for l in links), [l for l in links if not (ROOT / l).exists()]
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
    return DocStore(sqlite3.connect(":memory:"), threading.RLock())


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
    return guards.plan({"tasks": tasks}, oracle, list(reqs), tier, [".github/"], SOULS)


def test_plan_guards():
    assert check([_task("T1", ["src/a.py"]), _task("T2", ["src/b.py"], ["T1"])]) == []
    ids = lambda v: {x.split(":")[0] for x in v}
    assert "G-P2" in ids(check([_task("T1", ["a"], ["T2"]), _task("T2", ["b"], ["T1"])]))
    assert "G-P3" in ids(check([_task("T1", ["a"])], reqs=("REQ-1", "REQ-2")))
    assert "G-P6" in ids(check([_task("T1", ["src/"]), _task("T2", ["src/b.py"])]))   # parallel overlap
    assert check([_task("T1", ["src/"]), _task("T2", ["src/b.py"], ["T1"])]) == []    # ordered: fine
    assert "G-P7" in ids(check([_task("T1", ["tests/acc/test_a.py"])]))
    assert "G-P7" in ids(check([_task("T1", [".github/workflows/ci.yml"])]))
    assert "G-P6" in ids(check([_task("T1", ["../x"])]))
    assert "G-P8" in ids(check([_task("T1", ["a"])], oracle={"acceptance_tests": []}))
    assert check([_task("T1", ["a"])], tier="R1", oracle={}) == []
    nosurf = dict(ORACLE, surfaces=[])
    assert "G-P10" in ids(check([_task("T1", ["a"])], oracle=nosurf))
    badsurf = dict(ORACLE, surfaces=[{"id": "API", "commands": [], "pins": ["../x"]}])
    assert len([x for x in check([_task("T1", ["a"])], oracle=badsurf) if x.startswith("G-P10")]) == 3
    bad = _task("T1", ["a"]); bad["soul"] = "code-reviewer"
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
    from legion_orch.attest import normalize, qualify
    from legion_orch.mcp_tools import _hidden
    assert qualify("plan/spec.md", "c1") == "changes/c1/plan/spec.md"
    assert qualify("changes/c2/plan/spec.md", "c1") == "changes/c2/plan/spec.md"
    assert normalize("file:plan/spec.md@2", "c1") == "file:changes/c1/plan/spec.md@2"
    assert normalize(f"git:app@{SHA}", "c1") == f"git:app@{SHA}"
    assert _hidden("changes/c1/control/logs/r0001.log")
    assert not _hidden("changes/c1/control/approvals/spec.json")


def test_agents_cannot_write_other_changes_or_control(tmp_path):
    import types
    from legion_orch.engine import ChangeEngine, WorkflowError
    from legion_orch.model import Run
    docs = _store()
    ctl = types.SimpleNamespace(cfg=None, docs=docs, git=None, assets=None, runner=None, lock=None,
                                procs={}, read_tokens={}, version=0)
    ce = ChangeEngine(ctl, "c1")
    run = Run(id="r0001", node_id="plan", role="planner", soul="s", cycle=1, token="t",
              write_scope=["changes/c1/plan/"])
    ce.st._events = []
    ce.st.event = lambda *a, **k: 0
    assert ce.docs_write(run, "plan/spec.md", "x", None).path == "changes/c1/plan/spec.md"
    for bad in ("changes/c2/plan/spec.md", "control/approvals/x.json", "intake/requirements.md"):
        with pytest.raises(WorkflowError, match="G-W1"):
            ce.docs_write(run, bad, "x", None)


def test_review_panels():
    assert risk.review_panel("R3", {}) == ["reviewer", "security", "architecture"]
    assert risk.review_panel("R2", {"scope_breadth": 2}) == ["reviewer", "architecture"]
    assert risk.review_panel("R2", {}) == ["reviewer"]


def test_protected_path_approval_timing_and_coverage():
    import types
    from legion_orch import governance
    e = types.SimpleNamespace(change=types.SimpleNamespace(protected_approvals=[
        {"task": "T1", "paths": ["tests/acc/test_a.py"], "at": "2026-01-02T00:00:00.000Z"}]))
    assert governance.approved_protected_for(e, "T1", "2026-01-03T00:00:00.000Z") == ["tests/acc/test_a.py"]
    assert governance.approved_protected_for(e, "T1", "2026-01-01T00:00:00.000Z") == []   # run predates approval
    assert governance.approved_protected_for(e, "T2", "2026-01-03T00:00:00.000Z") == []   # other task
    assert governance.coverage_ok({"covers": None}, {"T1", "T2"})
    assert not governance.coverage_ok({"covers": ["T1"]}, {"T1", "T2"})
