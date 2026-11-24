"""Acceptance (out of process) for selectable workflows, the design-review workflow and the
codebase-design skill (AC-1 … AC-10). Nothing here imports legion_orch: every check runs a
subprocess (the CLI, a controller process via the protected driver, or a short python program in
its own interpreter) or reads a file. Criterion mapping: see tickets/oracle/tests.md."""
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / "tests" / "acceptance" / "support"
DRIVER = SUPPORT / "design_driver.py"
GOLDEN_SKILL = (SUPPORT / "golden" / "legion-workflow-default.md").read_text()
GOLDEN = json.loads((SUPPORT / "golden" / "default-skeleton.json").read_text())

DESIGN_REVIEW_IDS = ["inspect", "findings-review", "select", "H-S"]
# the skills of the roles that existed before this change (AC-6: unchanged)
EXISTING_SKILLS = {
    "intake": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "risk-signals", "blocked-protocol",
    ],
    "planner": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "plan-format", "sandbox-environment", "blocked-protocol", "language-preferences",
    ],
    "critic": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "pre-mortem", "finding-format", "language-preferences",
    ],
    "oracle": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "git-protocol", "blocked-protocol", "language-preferences",
    ],
    "implementer": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "git-protocol", "blocked-protocol", "language-preferences",
    ],
    "scaffolder": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "git-protocol", "blocked-protocol", "language-preferences",
    ],
    "reviewer": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "git-protocol", "finding-format", "language-preferences",
    ],
    "security": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "git-protocol", "finding-format", "security-checklist",
    ],
    "integration": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "git-protocol", "finding-format",
    ],
    "describer": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "git-protocol", "blocked-protocol",
    ],
    "pr-reviewer": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "git-protocol", "finding-format",
    ],
    "release": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
    ],
    "architecture": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "git-protocol", "finding-format", "architecture-checklist",
    ],
    "retrospective": [
        "harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
    ],
    "resolver": [
        "resolution-protocol", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format",
        "blocked-protocol",
    ],
}


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600, **kw)


def py(code: str, payload=None):
    """Run ``code`` in a separate interpreter; it prints one JSON value, returned here."""
    p = sh([sys.executable, "-c", textwrap.dedent(code)], input=json.dumps(payload))
    try:
        return json.loads(p.stdout)
    except ValueError:
        pytest.fail(f"program failed ({p.returncode}): {(p.stdout + p.stderr)[-2500:]}")


def write_config(tmp: Path, assets: Path) -> Path:
    repo = tmp / "src-repo"
    repo.mkdir(exist_ok=True)
    cfg = tmp / "orchestrator.yaml"
    cfg.write_text(textwrap.dedent(f"""
        project: design-acceptance
        workspaces: [{{name: app, source: {repo}, base_branch: main}}]
        decision_owners: [miles]
        state_dir: {tmp / 'state'}
        assets_dir: {assets}
        workflow_doc: {ROOT / 'WORKFLOW.md'}
        sandbox: {{runner: local, templates_dir: {ROOT / 'policies'}}}
    """))
    return cfg


def check(tmp: Path, assets: Path):
    p = sh([sys.executable, "-m", "legion_orch", "-c", str(write_config(tmp, assets)), "check"])
    return p.returncode, p.stdout + p.stderr


@pytest.fixture
def assets_copy(tmp_path):
    dst = tmp_path / "assets"
    shutil.copytree(ROOT / "assets", dst)
    return dst


def default_yaml(assets: Path) -> str:
    return (assets / "workflows" / "default.yaml").read_text()


# ===================================================================================== AC-1
def test_AC_1_the_default_lives_in_workflows_and_the_old_file_is_gone():
    assert (ROOT / "assets" / "workflows" / "default.yaml").is_file()
    assert not (ROOT / "assets" / "workflow.yaml").exists()


def test_AC_1_two_valid_workflows_load_and_check_names_both(tmp_path, assets_copy):
    (assets_copy / "workflows" / "second.yaml").write_text(default_yaml(assets_copy))
    code, out = check(tmp_path, assets_copy)
    assert code == 0, out[-2000:]
    assert "default" in out and "second" in out


@pytest.mark.parametrize("case", ["malformed", "unknown-role", "unknown-controller", "bad-name"])
def test_AC_1_a_bad_workflow_file_fails_check_naming_the_file(tmp_path, assets_copy, case):
    text = default_yaml(assets_copy)
    name = "broken"
    if case == "malformed":
        text = "skeleton: [unclosed\n  - : :\n"
    elif case == "unknown-role":
        assert "agent:intake" in text
        text = text.replace("agent:intake", "agent:no-such-role")
    elif case == "unknown-controller":
        assert "controller:sync" in text
        text = text.replace("controller:sync", "controller:teleport")
    else:
        name = "Bad_Name"
    (assets_copy / "workflows" / f"{name}.yaml").write_text(text)
    code, out = check(tmp_path, assets_copy)
    assert code != 0, out[-2000:]
    assert name in out, out[-2000:]


def test_AC_1_a_workflows_folder_without_default_fails(tmp_path, assets_copy):
    (assets_copy / "workflows" / "second.yaml").write_text(default_yaml(assets_copy))
    (assets_copy / "workflows" / "default.yaml").unlink()
    code, out = check(tmp_path, assets_copy)
    assert code != 0, out[-2000:]


# ============================================================ the controller over HTTP (AC-2, AC-3, AC-5)
def expected_design_review_start():
    start = GOLDEN["start_ids"]
    i = start.index("workspace") + 1
    return start[:i] + ["review"] + DESIGN_REVIEW_IDS + start[i:]


@pytest.fixture(scope="module")
def http():
    p = subprocess.run([sys.executable, str(DRIVER)], cwd=ROOT, capture_output=True, text=True, timeout=400,
                       input=json.dumps({"creates": [{"workflow": "design-review"}, {},
                                                     {"workflow": "no-such-workflow"}],
                                         "restart": True, "deadline_s": 240}))
    try:
        res = json.loads(p.stdout)
    except ValueError:
        pytest.fail(f"driver crashed ({p.returncode}): {p.stderr[-1500:]}")
    assert "error" not in res, res["error"]
    return res


def test_AC_2_http_a_change_under_design_review_has_its_tickets(http):
    dr = http["creates"][0]
    assert dr["http"] == 200
    for tid in DESIGN_REVIEW_IDS:
        assert tid in dr["tickets"], dr["tickets"]
    assert dr["workflow"] == "design-review"


def test_AC_2_http_without_a_workflow_the_tickets_are_the_default_skeletons(http):
    default = http["creates"][1]
    assert default["http"] == 200
    assert default["tickets"] == GOLDEN["start_ids"]
    assert "inspect" not in default["tickets"] and "select" not in default["tickets"]


def test_AC_2_http_an_unknown_workflow_is_rejected_and_creates_nothing(http):
    bad = http["creates"][2]
    assert 400 <= bad["http"] < 500
    assert len(http["changes_after"]) == len(http["changes_before"]) + 2
    assert not bad.get("change")


def test_AC_2_http_the_workflow_survives_a_restart_with_the_same_tickets(http):
    created = {c["change"]: c for c in http["creates"] if c.get("change")}
    assert len(http["restarted"]) == 2
    for r in http["restarted"]:
        assert r["tickets"] == created[r["change"]]["tickets"]
        assert r["workflow"] == created[r["change"]]["workflow"]
    assert http["restarted"][0]["workflow"] == "design-review"


def test_AC_3_http_meta_lists_both_workflow_names(http):
    assert {"default", "design-review"} <= set(http["meta"]["workflows"])


def test_AC_5_http_a_design_review_change_starts_with_no_build_side_tickets(http):
    dr = http["creates"][0]["tickets"]
    assert dr == expected_design_review_start()
    for t in ("oracle", "baseline", "preflight"):
        assert t not in dr


# ===================================================================================== AC-3
RENDER = """
    import json, sys
    from pathlib import Path
    from legion_orch.workflow.assets import Assets, TEMPLATE_VARS
    a = Assets(Path(@DIR@))
    v = {k: "x" for k in TEMPLATE_VARS}
    out = {}
    for wf in ("default", "design-review"):
        r = a.render_role("planner", dict(v), workflow=wf)
        out[wf] = {"md": r.skills["legion-workflow"], "pin": r.pin, "body": a.workflow_skill(wf)}
    print(json.dumps(out))
""".replace("@DIR@", repr(str(ROOT / "assets")))


def test_AC_3_the_planner_prompt_describes_the_changes_own_workflow():
    out = py(RENDER)
    dr, default = out["design-review"], out["default"]
    for tid in ("inspect", "select"):
        assert f"`{tid}`" in dr["md"]
        assert f"`{tid}`" not in default["md"]
    assert dr["md"] != default["md"]
    assert dr["pin"] != default["pin"]


def test_AC_3_the_default_skill_text_equals_the_golden_file():
    default = py(RENDER)["default"]
    assert default["body"] == GOLDEN_SKILL
    assert default["md"].endswith(GOLDEN_SKILL)


# ===================================================================================== AC-4
SKILL = ROOT / "assets" / "skills" / "codebase-design.md"


def test_AC_4_the_skill_file_has_description_only_attribution_and_a_licence():
    text = SKILL.read_text()
    assert text.startswith("---\n")
    head, sep, body = text[4:].partition("\n---\n")
    assert sep
    meta = yaml.safe_load(head)
    assert set(meta) == {"description"}
    assert isinstance(meta["description"], str) and 0 < len(meta["description"].strip()) <= 1024
    assert "{{" not in text and "}}" not in text
    assert "mattpocock/skills" in text
    assert re.search(r"(?i)licen[cs]e", body)


def test_AC_4_the_assets_load_and_the_skill_renders_as_legion_codebase_design():
    out = py("""
        import json
        from pathlib import Path
        from legion_orch.workflow.assets import Assets, TEMPLATE_VARS
        a = Assets(Path(@DIR@))
        r = a.render_role("planner", {k: "x" for k in TEMPLATE_VARS})
        print(json.dumps({"md": r.skills.get("legion-codebase-design", ""),
                          "desc": a.skill_descriptions.get("codebase-design", "")}))
    """.replace("@DIR@", repr(str(ROOT / "assets"))))
    assert out["md"].strip() and out["desc"].strip()
    assert "legion-codebase-design" in out["md"]


# ===================================================================================== AC-5
SPEC_DUMP = """
    import json, sys
    from pathlib import Path
    from legion_orch.workflow.tickets.spec import WorkflowSpec
    out = {}
    for name in ("default", "design-review"):
        s = WorkflowSpec.load(Path(@DIR@) / (name + ".yaml"))
        out[name] = {
            "walk": [[t.id, t.type] for t in s.skeleton.walk()],
            "signs": {t.id: t.signs for t in s.skeleton.walk()},
            "inputs": {t.id: t.inputs for t in s.skeleton.walk()},
            "tiers": {n: s.policy(n).model_dump(mode="json") for n in s.tiers},
            "hooks": s.hooks, "stale": s.stale,
            "criteria": {k: v.model_dump() for k, v in s.criteria.items()},
        }
    print(json.dumps(out))
""".replace("@DIR@", repr(str(ROOT / "assets" / "workflows")))


@pytest.fixture(scope="module")
def specs():
    return py(SPEC_DUMP)


def test_AC_5_the_ticket_order_is_the_defaults_with_the_review_inserted(specs):
    walk = specs["design-review"]["walk"]
    ids = [i for i, _ in walk]
    default_ids = [i for i, _ in GOLDEN["skeleton"]]
    i = default_ids.index("workspace") + 1
    assert ids == default_ids[:i] + ["review"] + DESIGN_REVIEW_IDS + default_ids[i:]
    types = dict(walk)
    assert types["review"] == "grouping"
    assert types["inspect"] == "agent:design-reviewer"
    assert types["findings-review"] == "agent:findings-reviewer"
    assert types["select"] == "agent:selector"
    assert types["H-S"] == "human:signoff"


def test_AC_5_H_S_signs_selection_and_cannot_be_waived(specs):
    dr = specs["design-review"]
    assert dr["signs"]["H-S"] == ["selection"]
    assert dr["criteria"]["human:signoff"]["waivable"] is False
    assert "selection" in dr["inputs"]["intake"]
    assert "selection" in dr["inputs"]["design"]


def test_AC_5_policies_hooks_and_the_rest_are_the_defaults(specs):
    dr = specs["design-review"]
    assert dr["tiers"] == GOLDEN["tiers"]
    assert dr["hooks"] == GOLDEN["hooks"]
    assert dr["stale"] == GOLDEN["stale"]
    assert dr["criteria"] == GOLDEN["criteria"]


# ===================================================================================== AC-6
def test_AC_6_the_new_roles_have_souls_prompts_and_handlers():
    roles = yaml.safe_load((ROOT / "assets" / "roles.yaml").read_text())
    for r in ("design-reviewer", "findings-reviewer", "selector"):
        assert r in roles
        assert (ROOT / "assets" / "souls" / f"{roles[r]['soul']}.md").is_file()
        assert (ROOT / "assets" / "prompts" / f"{r}.md").is_file()
    registered = py("""
        import json
        import legion_orch.workflow.roles as roles
        print(json.dumps(sorted(roles.REGISTRY)))
    """)
    assert set(roles) <= set(registered)


def test_AC_6_codebase_design_belongs_to_the_design_reviewer_alone_and_old_skills_are_unchanged():
    roles = yaml.safe_load((ROOT / "assets" / "roles.yaml").read_text())
    assert "codebase-design" in roles["design-reviewer"]["skills"]
    assert [r for r, v in roles.items() if "codebase-design" in v["skills"]] == ["design-reviewer"]
    for name, skills in EXISTING_SKILLS.items():
        assert roles[name]["skills"] == skills, name


def test_AC_6_legion_orch_check_passes(tmp_path):
    code, out = check(tmp_path, ROOT / "assets")
    assert code == 0, out[-2000:]


# ===================================================================================== AC-7
PROGRAM = """
    import json, sys
    from pathlib import Path
    from legion_orch.store import SchemaRegistry
    from legion_orch.workflow import guards as g
    reg = SchemaRegistry(Path(@DIR@))
    out = []
    for c in json.load(sys.stdin):
        try:
            if c["fn"] == "schema":
                reg.check(c["path"], json.dumps(c["doc"]))
                out.append({"ok": True})
            else:
                args = [set(a["set"]) if isinstance(a, dict) and "set" in a else a for a in c["args"]]
                out.append({"problems": getattr(g, c["fn"])(*args)})
        except Exception as e:
            out.append({"ok": False, "error": f"{type(e).__name__}: {e}"})
    print(json.dumps(out))
""".replace("@DIR@", repr(str(ROOT / "assets" / "schemas")))

EV1, EV2 = "git:app/src/a.py@abcdef1", "git:app/src/b.py@abcdef2"


def finding(i, ev=None, **kw):
    f = {"id": f"F-{i}", "title": f"title {i}", "evidence": ev or [EV1], "problem": "p",
         "direction": "d", "severity": "high", "effort": "small"}
    return f | kw


def drop(d, key):
    return {k: v for k, v in d.items() if k != key}


FINDINGS = {"findings": [finding(1), finding(2, [EV2])]}
REVIEW = {"reviews": [{"finding": "F-1", "verdict": "valid", "severity": "high", "note": "ok"},
                      {"finding": "F-2", "verdict": "invalid", "severity": "low", "note": "no"}]}
SELECTION = {"selected": [{"id": "F-1", "reason": "r", "scope": "s", "evidence": [EV1]}],
             "dropped": [{"id": "F-2", "reason": "r"}]}
REQS = "- REQ-1: fix the thing (F-1)\n"


def run(cases):
    return py(PROGRAM, cases)


def test_AC_7_the_schema_store_accepts_consistent_documents_and_rejects_malformed_ones():
    p = "tickets/inspect/%s.json"
    cases = [
        {"fn": "schema", "path": p % "findings", "doc": FINDINGS},
        {"fn": "schema", "path": p % "findings", "doc": {"findings": [drop(finding(1), "id")]}},
        {"fn": "schema", "path": p % "findings", "doc": {"findings": [drop(finding(1), "evidence")]}},
        {"fn": "schema", "path": p % "findings", "doc": {"findings": [finding(1, ["src/a.py"])]}},
        {"fn": "schema", "path": "tickets/findings-review/findings-review.json", "doc": REVIEW},
        {"fn": "schema", "path": "tickets/findings-review/findings-review.json",
         "doc": {"reviews": [{"finding": "F-1", "verdict": "maybe", "severity": "high", "note": "n"}]}},
        {"fn": "schema", "path": "tickets/select/selection.json", "doc": SELECTION},
        {"fn": "schema", "path": "tickets/select/selection.json",
         "doc": {"selected": [{"id": "F-1", "reason": "r"}], "dropped": []}},
    ]
    got = [r["ok"] for r in run(cases)]
    assert got == [True, False, False, False, True, False, True, False]


def test_AC_7_the_findings_guard_rejects_duplicates_and_unattested_evidence():
    att = {"set": [EV1, EV2]}
    dup = {"findings": [finding(1), finding(1)]}
    off = {"findings": [finding(1, ["git:app/other.py@abcdef9"])]}
    res = run([{"fn": "findings_problems", "args": [FINDINGS, att]},
               {"fn": "findings_problems", "args": [dup, att]},
               {"fn": "findings_problems", "args": [off, att]}])
    assert res[0]["problems"] == []
    for r in res[1:]:
        assert r["problems"] and r["problems"][0].startswith("G-DR1")


def test_AC_7_the_review_guard_rejects_an_unknown_finding_id():
    unknown = {"reviews": REVIEW["reviews"] + [{"finding": "F-9", "verdict": "valid",
                                                "severity": "low", "note": "n"}]}
    res = run([{"fn": "review_problems", "args": [REVIEW, FINDINGS]},
               {"fn": "review_problems", "args": [unknown, FINDINGS]}])
    assert res[0]["problems"] == []
    assert res[1]["problems"] and res[1]["problems"][0].startswith("G-DR2")


def test_AC_7_the_selection_guard_rejects_unknown_ids_duplicates_and_invalid_findings():
    unknown = {"selected": SELECTION["selected"], "dropped": [{"id": "F-9", "reason": "r"}]}
    dup = {"selected": SELECTION["selected"] * 2, "dropped": SELECTION["dropped"]}
    invalid = {"selected": SELECTION["selected"]
               + [{"id": "F-2", "reason": "r", "scope": "s", "evidence": [EV2]}], "dropped": []}
    res = run([{"fn": "selection_problems", "args": [s, FINDINGS, REVIEW]}
               for s in (SELECTION, unknown, dup, invalid)])
    assert res[0]["problems"] == []
    for r in res[1:]:
        assert r["problems"] and r["problems"][0].startswith("G-DR3")


def test_AC_7_the_requirements_guard_rejects_unselected_citations_and_uncovered_selections():
    both = "- REQ-1: a (F-1)\n- REQ-2: b (F-2)\n"
    none = "- REQ-1: something with no finding\n"
    res = run([{"fn": "requirements_problems", "args": [t, SELECTION]} for t in (REQS, both, none)]
              + [{"fn": "requirements_problems", "args": ["", SELECTION]}])
    assert res[0]["problems"] == []
    for r in res[1:]:
        assert r["problems"] and r["problems"][0].startswith("G-DR4")


# ===================================================================================== AC-8
def test_AC_8_the_default_workflow_equals_the_pre_change_one(specs):
    d = specs["default"]
    assert d["walk"] == GOLDEN["skeleton"]
    assert d["tiers"] == GOLDEN["tiers"]
    assert d["hooks"] == GOLDEN["hooks"]
    assert d["stale"] == GOLDEN["stale"]
    assert d["criteria"] == GOLDEN["criteria"]


# ===================================================================================== AC-9
def test_AC_9_the_docs_mention_workflows_design_review_and_codebase_design():
    for doc in ("WORKFLOW.md", "README.md"):
        text = (ROOT / doc).read_text()
        for needle in ("assets/workflows/", "design-review", "codebase-design"):
            assert needle in text, (doc, needle)


@pytest.mark.parametrize("gid", ["G-DR1", "G-DR2", "G-DR3", "G-DR4"])
def test_AC_9_workflow_md_documents_the_design_guards(gid):
    text = (ROOT / "WORKFLOW.md").read_text()
    assert re.search(r"\|\s*" + gid + r"\s*\|", text), gid


# ==================================================================================== AC-10
NEW_TESTS = ["tests/test_workflows.py", "tests/test_codebase_design_skill.py",
             "tests/test_design_schemas.py", "tests/test_design_roles.py",
             "tests/test_workflow_selection.py", "tests/test_design_review_workflow.py"]


@pytest.mark.parametrize("path", NEW_TESTS)
def test_AC_10_each_new_test_file_exists_and_collects_four_tests(path):
    assert (ROOT / path).is_file()
    p = sh([sys.executable, "-m", "pytest", "--collect-only", "-q", "-n", "0",
            "-p", "no:cacheprovider", path])
    n = sum(1 for line in p.stdout.splitlines() if line.startswith(path + "::"))
    assert n >= 4, p.stdout[-1500:] + p.stderr[-800:]
