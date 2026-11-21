"""Gate screens (WORKFLOW.md §2.3): the traceability matrix and its warnings, the diff of what
H-3 accepts, document diffs since approval, G-P12 and the CLI's markdown form."""
from __future__ import annotations

import httpx
from engine_harness import ORACLE, TASKS, Flow, scenario

from legion_orch.server.web import build_app
from legion_orch.workflow import governance, traceability
from legion_orch.workflow.guards.plan import criterion_links

TEST, CMD = ORACLE["acceptance_tests"][0], ORACLE["acceptance_commands"][0]
LINKED = ORACLE | {"criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "hello",
                                 "tests": [TEST], "commands": [CMD]}]}
REASON = "reviewed by hand against the production incident log"


def client(f: Flow) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(f.ctl)),
                             base_url="http://op")


def kinds(t) -> list[str]:
    return [w.kind for w in t.warnings]


# ====================================================================== the matrix at H-2
@scenario()
async def test_the_matrix_at_h2_traces_requirements_to_tasks_and_criteria(f: Flow):
    await f.to_h2(tier="R2")
    async with client(f) as c:
        t = (await c.get(f"/api/changes/{f.ce.cid}/trace")).json()
    [req] = t["requirements"]
    assert req["id"] == "REQ-1" and req["text"] == "greet.txt says hello"
    assert req["tasks"] == ["T1"] and req["spec"]["status"] == "approved"     # at H-E1
    assert [c["id"] for c in req["criteria"]] == ["AC-1"]
    [task] = t["tasks"]
    assert task["node"] == "build:T1" and task["status"] == "PENDING" and task["check"] is None
    assert not t["built"] and t["results"] is None
    assert t["summary"] == {"requirements": 1, "tasks": 1, "criteria": 1, "warnings": 1}
    assert [w["kind"] for w in t["warnings"]] == ["unlinked_criterion"]


@scenario()
async def test_warnings_for_gaps_the_guards_allow(f: Flow):
    await f.intake(reqs="- REQ-1: greet.txt says hello\n- **REQ-2**: and it is lint clean\n")
    await f.human("H-1", "accept", tier="R2")
    tasks = [TASKS[0] | {"requirements": ["REQ-1", "REQ-2"],
                         "verification": ["ruff check src", "npx tsc --noEmit"]}]
    await f.plan(tasks=tasks, oracle=LINKED)
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    await f.oracle()
    t = traceability.traceability(f.ce)
    assert [r.text for r in t.requirements] == ["greet.txt says hello", "and it is lint clean"]
    assert kinds(t) == ["no_criterion", "lint_only"]       # REQ-2 untested; T1 only lints
    assert t.warnings[0].requirement == "REQ-2"
    [ac] = t.requirements[0].criteria
    assert ac.tests == [TEST] and ac.commands == [CMD] and ac.result is None


def test_lint_only_commands():
    assert traceability.lint_only(["ruff check .", "mypy src"])
    assert not traceability.lint_only(["ruff check .", "pytest -q"])
    assert not traceability.lint_only(["bun run lint && bun test"])
    assert not traceability.lint_only(["grep -q hello src/greet.txt"])
    assert not traceability.lint_only([])


@scenario()
async def test_a_spec_changed_since_approval_shows_its_diff(f: Flow):
    await f.to_h2(tier="R2")                           # H-E1 approved REQ-1's delta spec
    rel = "plan/specs/REQ-1.md"
    old = f.ce.latest(rel)
    new = f.ce.doc(rel).content.replace("add it", "add it, politely")
    f.ce.write_record(rel, new)
    [w] = [w for w in traceability.traceability(f.ce).warnings if w.kind == "stale"]
    assert w.requirement == "REQ-1" and w.version == old and w.path == f.ce.p(rel)
    assert f"v{old}" in w.message and "miles" in w.message
    async with client(f) as c:
        d = (await c.get("/api/doc/diff", params={"path": w.path, "from": old})).json()
        assert d["from"] == old and d["to"] == old + 1
        assert "-add it\n" in d["diff"] and "+add it, politely\n" in d["diff"]
        assert (await c.get("/api/doc/diff", params={"path": w.path})).status_code == 400
        h2 = (await c.get(f"/api/changes/{f.ce.cid}/state")).json()["h2"]
        spec = next(s for s in h2 if s["subject"] == "REQ-1")
        assert spec["status"] == "stale" and spec["approved"][w.path] == old
        assert spec["latest"][w.path] == old + 1


# ====================================================================== H-3
@scenario()
async def test_at_h3_the_matrix_has_results_and_the_diff_per_task(f: Flow):
    await f.to_build(tier="R2", oracle=LINKED)
    await f.implement(files={"src/greet.txt": "hello\nworld\n"})
    await f.review()
    await f.integration()
    assert f.status("H-3") == "AWAITING_HUMAN", f.status()
    t = traceability.traceability(f.ce)
    assert t.built and t.results and t.results.verdict == "PASS"
    assert CMD in [c.command for c in t.results.commands]
    [task] = t.tasks
    assert task.status == "ACCEPTED" and task.check and task.check.ok
    assert [(r.role, r.verdict) for r in task.reviews] == [("reviewer", "PASS")]
    assert t.requirements[0].criteria[0].result == "pass"
    assert t.warnings == []
    async with client(f) as c:
        a = (await c.get(f"/api/changes/{f.ce.cid}/acceptance")).json()
    assert a["error"] is None
    d = a["diff"]
    assert d["head"] == f.ce.change.integration_head and d["base"] == f.ce.change.base_sha
    [td] = d["tasks"]
    [fs] = td["files"]
    assert td["id"] == "T1" and fs["path"] == "src/greet.txt" and fs["added"] == 2
    assert {x["path"] for x in d["files"]} >= {"src/greet.txt", TEST}    # the oracle's tests too
    assert d["protected"] == []
    r = a["readiness"]
    assert r["verdict"] == "NOT_READY"                  # H-3 itself is not accepted yet
    assert set(r["gates"]) == set(governance.gate_set(f.ce))
    assert r["gates"]["whole_change_acceptance_evidence"]["status"] != "satisfied"


@scenario()
async def test_the_diff_lists_protected_paths_a_task_changed_with_their_approval(f: Flow):
    await f.to_h2(tier="R2")
    await f.change_action("approve_protected_paths", task="T1", paths=[TEST], reason=REASON)
    await f.human("H-2", "approve")
    await f.implement(files={"src/greet.txt": "hello\n", TEST: "grep -q hello src/greet.txt\n#\n"})
    await f.review()
    await f.integration()
    d = await traceability.diffstat(f.ce)
    assert [(p.path, p.approved_by, p.tasks) for p in d.protected] == [(TEST, ["miles"], ["T1"])]


# ====================================================================== G-P12 and the CLI
def test_g_p12_checks_criterion_links_against_the_oracle():
    assert criterion_links(LINKED) == []
    assert criterion_links(ORACLE) == []                # links are optional
    bad = ORACLE | {"criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "x",
                                  "tests": ["tests/other.sh"], "commands": ["make test"]}]}
    v = criterion_links(bad)
    assert len(v) == 2 and all(x.startswith("G-P12: criterion AC-1") for x in v)


@scenario()
async def test_g_p12_rejects_a_plan_linking_an_unknown_test(f: Flow):
    await f.intake()
    await f.human("H-1", "accept", tier="R2")
    bad = ORACLE | {"criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "x",
                                  "tests": ["tests/other.sh"]}]}
    out = await f.plan(oracle=bad)
    assert not out.accepted and any("G-P12" in v for v in out.violations), out


@scenario()
async def test_trace_markdown(f: Flow):
    await f.to_h2(tier="R2")
    md = traceability.to_markdown(traceability.traceability(f.ce).model_dump(mode="json"), f.ce.cid)
    assert md.startswith(f"# Traceability: {f.ce.cid}\n")
    assert "1 requirements · 1 tasks · 1 criteria · 1 warnings" in md
    assert "| **REQ-1** greet.txt says hello | approved | T1 (PENDING) | AC-1: hello → unlinked |" in md
    assert "AC-1 names no test or command" in md
