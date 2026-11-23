"""A blank repository's scaffold task (WORKFLOW.md §5.3b): the planner plans one, and it is
built, verified, reviewed and merged before the oracle and H-2, so the acceptance tests are
written against a real project."""
from __future__ import annotations

import pytest
from engine_harness import ORACLE, REPO, ROOT, TASKS, Flow, scenario

from legion_orch.git import is_blank_tree
from legion_orch.store import SchemaRegistry
from legion_orch.workflow import guards
from legion_orch.workflow.model import (
    ACCEPTED,
    AWAITING_HUMAN,
    PENDING,
    RUNNING,
    ChangeStatus,
)

SCAFFOLD = {"id": "T1", "title": "scaffold the project", "soul": "senior-developer",
            "requirements": ["REQ-1"], "depends_on": [], "files_modified": ["Makefile"],
            "instructions": "add a Makefile with a test target", "acceptance": ["make test runs"],
            "verification": ["make -n test"], "scaffold": True}
GREET = dict(TASKS[0], id="T2", depends_on=["T1"])
PLAN = [SCAFFOLD, GREET]
SCHEMAS = SchemaRegistry(ROOT / "assets/schemas")
SOULS = ["senior-developer", "backend-engineer"]


def _plan_guards(tasks, blank_repo: bool) -> list[str]:
    return guards.plan({"tasks": tasks}, ORACLE, ["REQ-1"], "R1", [], SOULS, SCHEMAS,
                       blank_repo=blank_repo)


@pytest.mark.parametrize(("paths", "blank"), [
    ([], True),
    (["README.md", "LICENSE", ".gitignore", "docs/design.md", ".github/workflows/ci.yml"], True),
    (["README.md", "pyproject.toml"], False),
    (["README.md", "src/greet.txt"], False),
    (["docs/LICENSE"], False),                 # licence files count only at the top level
    (["README.md", "mise.toml", ".tool-versions"], True),    # tool versions are not code
    (["README.md", ".mise.toml"], True),
    (["config/mise.toml"], False),             # mise files count only at the top level
])
def test_a_tree_is_blank_with_only_documentation_and_settings(paths, blank):
    assert is_blank_tree(paths) is blank


@pytest.mark.parametrize(("tasks", "blank", "expect"), [
    (PLAN, True, []),
    ([dict(SCAFFOLD, scaffold=False), GREET], True, ["exactly one task"]),
    ([SCAFFOLD, dict(GREET, scaffold=True)], True, ["exactly one task"]),
    ([dict(SCAFFOLD, depends_on=["T2"]), dict(GREET, depends_on=[])], True,
     ["must not depend", "T2 must depend on scaffold task T1"]),
    ([SCAFFOLD, dict(GREET, depends_on=[]), dict(GREET, id="T3", depends_on=["T2"])], True,
     ["T2 must depend", "T3 must depend"]),
    ([SCAFFOLD, GREET, dict(GREET, id="T3", depends_on=["T2"],
                            files_modified=["src/other.txt"])], True, []),   # transitively
    (PLAN, False, ["not blank"]),
    (TASKS, False, []),
])
def test_g_p11_one_scaffold_first_in_a_blank_repository(tasks, blank, expect):
    v = [x for x in _plan_guards(tasks, blank) if x.startswith("G-P11")]
    assert len(v) == len(expect) and all(e in x for e, x in zip(expect, v, strict=True)), v


def test_a_fix_round_never_adds_a_scaffold():
    built = {t["id"]: t for t in PLAN}
    fix = dict(GREET, id="T3", depends_on=["T2"], scaffold=True)
    v = guards.fix_round({"tasks": [*PLAN, fix]}, ORACLE, ["REQ-1"], "R1", [], SOULS, SCHEMAS,
                         built)
    assert v == ["G-P11: task T3 is marked scaffold; only the first round of a blank "
                 "repository has a scaffold task"]
    # a built task's contract is compared with its defaults: an omitted `scaffold` is false
    v = guards.fix_round({"tasks": [*PLAN, dict(fix, scaffold=False)]}, ORACLE, ["REQ-1"], "R1",
                         [], SOULS, SCHEMAS, built | {"T2": dict(GREET, scaffold=False)})
    assert v == []


@scenario()
async def test_a_repository_with_code_has_no_scaffold(f: Flow):
    assert f.ce.change.blank_repo is False
    await f.intake()
    await f.human("H-1", "accept")
    assert f.run("plan").template_vars["scaffold"] == ""
    out = await f.plan(tasks=PLAN)
    assert not out.accepted and any("G-P11" in v and "not blank" in v for v in out.violations)


@scenario(blank_repo=True)
async def test_the_scaffold_is_built_before_the_oracle_and_h2(f: Flow):
    ch = f.ce.change
    assert ch.blank_repo is True
    await f.intake()
    await f.human("H-1", "accept", tier="R2")
    plan = f.run("plan")
    assert "## Blank repository" in plan.template_vars["scaffold"]
    out = await f.plan()                               # no scaffold task: G-P11
    assert not out.accepted and any("G-P11" in v for v in out.violations), out
    out = await f.plan(tasks=PLAN)
    assert out.accepted, out.violations
    assert "## Blank repository" in f.run("plan", "critic").template_vars["scaffold"]
    await f.critique("PASS")
    s, oracle, h2 = (f.ce.nodes[n] for n in ("build:T1", "oracle", "H-2"))
    order = list(f.ce.nodes)        # the scaffold opens the oracle stage, T2 the delivery
    assert order.index("H-E1") < order.index("build:T1") < order.index("oracle") \
        < order.index("H-2") < order.index("build:T2")
    assert s.status == PENDING
    await f.human("H-E1", "approve")
    # built once the plan is approved: no oracle approval yet (G-B1 does not apply), and not a
    # "first build"
    assert s.status == RUNNING and oracle.status == PENDING and h2.status == PENDING
    assert not ch.approvals.get("oracle") and ch.first_build_at is None
    impl = f.run("build:T1", "implementer")
    assert f.ce.latest_att("plan/taskgraph.json") in impl.context_refs   # the approved plan
    await f.implement("T1", files={"Makefile": "test:\n\ttrue\n"})
    await f.review("T1")
    assert s.status == ACCEPTED and ch.integration_head != ch.base_sha
    assert await f.git.blob(ch.integration_head, "Makefile")
    # the oracle works at the head with the scaffold, and is told so
    orc = f.run("oracle")
    assert orc.base_sha == ch.integration_head
    assert f"git:{REPO}@{s.accepted_sha}" in orc.context_refs
    note = orc.template_vars["scaffold"]
    assert "## Scaffold" in note and s.accepted_sha[:12] in note
    await f.oracle()
    assert f.status("H-2") == AWAITING_HUMAN and ch.first_build_at is None
    await f.human("H-2", "approve")
    assert f.status("build:T2") == RUNNING and ch.first_build_at
    await f.implement("T2")
    await f.review("T2")
    await f.integration("PASS")
    await f.human("H-3", "accept")
    assert ch.status == ChangeStatus.READY, ch.ship
    assert "2 task contract(s)" in ch.ship.gates["task_contract"].detail


@scenario(blank_repo=True)
async def test_at_r1_the_scaffold_still_precedes_h2(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    assert f.ce.change.tier == "R1" and "oracle" not in f.ce.nodes
    await f.plan(tasks=PLAN)
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    assert f.status("build:T1") == RUNNING and f.status("build:T2") == PENDING
    assert "H-2" not in f.ce.nodes                 # R1: no oracle to approve (automatic gate)
    await f.implement("T1")
    await f.review("T1")
    assert f.status("build:T2") == RUNNING
    await f.implement("T2")
    await f.review("T2")
    assert f.ce.change.status == ChangeStatus.READY, f.ce.change.ship


@scenario(blank_repo=True, environment={})
async def test_with_the_environment_stage_the_scaffold_follows_provisioning(f: Flow):
    await f.intake()
    await f.human("H-1", "accept", tier="R2")
    await f.plan(tasks=PLAN)
    await f.critique("PASS")
    order = list(f.ce.nodes)
    assert order.index("build:T1") == order.index("provision") + 1
    assert f.status("build:T1") == PENDING
    await f.human("H-E1", "approve")
    assert f.status("provision") == ACCEPTED and f.status("build:T1") == RUNNING


@scenario(blank_repo=True)
async def test_restart_plan_drops_the_scaffold_and_builds_it_again(f: Flow):
    await f.intake()
    await f.human("H-1", "accept", tier="R2")
    await f.plan(tasks=PLAN)
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    await f.implement("T1")
    await f.review("T1")
    await f.oracle()
    ch = f.ce.change
    await f.human("H-2", "restart", feedback="use a different test runner")
    assert f.status("build:T1") == PENDING and ch.integration_head == ch.base_sha
    renamed = dict(SCAFFOLD, id="T3", title="scaffold with the other runner")
    await f.plan(tasks=[renamed, dict(GREET, depends_on=["T3"])])
    await f.critique("PASS")
    assert "build:T1" not in f.ce.nodes and "build:T3" in f.ce.nodes
    await f.human("H-E1", "approve")
    assert f.status("build:T3") == RUNNING and f.status("oracle") == PENDING


@pytest.mark.anyio
async def test_a_blank_repository_does_not_pin_its_base_setup(tmp_path):
    """README + mise.toml is blank (it gets its scaffold), and the base's setup task is not
    pinned: what it needs (a package.json) comes with the scaffold."""
    import subprocess
    path = tmp_path / "src-repo"
    path.mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    (path / "README.md").write_text("demo\n")
    (path / "mise.toml").write_text('[tools]\nbun = "1.4"\n\n[tasks.setup]\nrun = "bun install"\n')
    g("add", "-A")
    g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")

    f = await Flow.create(tmp_path)
    assert f.ce.change.blank_repo and f.ce.change.setup_pin is None


@scenario(blank_repo=True, environment={})
async def test_in_a_blank_repository_the_setup_starts_with_the_scaffold(f: Flow):
    """The approved setup is not checked at provision (there is nothing to install yet) and does
    not run in the scaffold's sandbox; the scaffold's verification runs it at its candidate, and
    every sandbox after the scaffold runs it."""
    import copy

    from engine_harness import ENVIRONMENT
    spec = copy.deepcopy(ENVIRONMENT)
    spec["profiles"]["check"]["offline"] = False
    spec |= {"mise": {"content": '[tools]\npython = "3.12"\n\n[tasks.setup]\nrun = "make deps"\n'},
             "setup_network": ["pypi"]}
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan(tasks=PLAN, environment=spec)
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    assert f.status("provision") == ACCEPTED and f.ce.change.setup_pin.source == "H-E1"
    assert any("checked at the scaffold's verification" in e["message"]
               for e in f.ce.timeline_events())
    scaffold = f.run("build:T1", "implementer")
    assert not any(s[0] == "mise-setup.sh" for s in f.steps(scaffold))
    await f.implement("T1")
    ver = f.ce.nodes["build:T1"].verification["1"].results
    assert (ver[0].command, ver[0].setup) == ("mise run setup", True)    # at the candidate
    await f.review("T1")
    assert f.status("build:T1") == ACCEPTED       # R1: on to the tasks (no oracle to approve)
    later = f.run("build:T2", "implementer")
    assert f.steps(later)[-1] == ["mise-setup.sh", f.ce.change.setup_pin.blob]
