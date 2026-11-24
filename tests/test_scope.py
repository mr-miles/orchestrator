"""A build's file scope (G-B3, WORKFLOW.md §5.6): a hard limit, grown only by a decision owner
– a build asks the orchestrator for more files (A2A ``request_scope``), a human approves or
denies it, and an approval adds the paths to its ``files_modified``."""
from __future__ import annotations

import pytest
from engine_harness import TASKS, Flow, scenario, signals
from test_a2a import client, rpc, send, task_when, token

from legion_orch.workflow import attention
from legion_orch.workflow.engine import WorkflowError

WHY = "the new endpoint must be registered in the route table"


def files(f: Flow, task: str = "T1", **extra: str) -> dict[str, str]:
    return {p: f"hello from {task}\n" for p in f.ticket(task).work["files_modified"]} | extra


async def ask(f: Flow, paths: list[str], reason: str = WHY, task: str = "T1"):
    run = f.run(task, "implementer")
    async with f.ce.lock:
        return f.ce.request_scope(f"q-{len(f.ce.change.questions)}", task, "implementer", run.id,
                                  paths, reason)


@scenario()
async def test_a_change_outside_the_scope_is_refused_and_says_how_to_ask(f: Flow):
    await f.to_build()
    out = await f.implement(files=files(f, **{"src/routes.txt": "greet\n"}))
    assert not out.accepted
    assert "G-B3: src/routes.txt is outside the allowed paths" in out.violations
    assert any("/request_scope <paths>" in v for v in out.violations), out.violations


@scenario()
async def test_an_approved_request_adds_the_paths_and_the_build_may_change_them(f: Flow):
    await f.to_build()
    before = list(f.ticket("T1").work["files_modified"])
    q = await ask(f, ["src/routes.txt"])
    assert (q.kind, q.paths, q.before, q.warnings) == ("scope", ["src/routes.txt"], before, [])
    assert "T1 asks for more files" in [i.title for i in attention.items(f.ce)]
    with pytest.raises(WorkflowError, match="approved or denied"):
        await f.ce.answer_question(q.id, "fine", "miles")
    with pytest.raises(WorkflowError, match="denial needs a reason"):
        await f.ce.answer_question(q.id, "", "miles", "deny")
    q = await f.ce.answer_question(q.id, "", "miles", "approve")
    assert q.approved and "you may change src/routes.txt" in q.answer
    t1 = f.ticket("T1")
    assert t1.work["files_modified"] == [*before, "src/routes.txt"]
    assert t1.scope_added == ["src/routes.txt"]
    out = await f.implement(files=files(f, **{"src/routes.txt": "greet\n"}))
    assert out.accepted, out.violations
    async with f.ce.lock:                                          # in its scope already
        assert f.ce.request_scope("q-again", "T1", "implementer", None, ["src/routes.txt"],
                                  WHY) is None


@scenario()
async def test_a_denied_request_keeps_the_limit(f: Flow):
    await f.to_build()
    q = await ask(f, ["src/routes.txt"])
    q = await f.ce.answer_question(q.id, "register it in T2 instead", "miles", "deny")
    assert q.approved is False and "register it in T2 instead" in q.answer
    assert "src/routes.txt" not in f.ticket("T1").work["files_modified"]
    out = await f.implement(files=files(f, **{"src/routes.txt": "greet\n"}))
    assert not out.accepted


@scenario()
async def test_protected_and_forbidden_paths_are_granted_only_by_a_decision_and_flagged(f: Flow):
    f.cfg.protected_paths = ["config/"]
    await f.to_build(tasks=[{**TASKS[0], "files_forbidden": ["src/secret.txt"]}])
    q = await ask(f, ["config/app.ini", "src/secret.txt"])
    assert any("config/app.ini is a protected path" in w for w in q.warnings)
    assert any("src/secret.txt is forbidden for this build" in w for w in q.warnings)
    out = await f.implement(files=files(f, **{"config/app.ini": "x\n", "src/secret.txt": "y\n"}))
    assert "G-B3: config/app.ini is a protected path" in out.violations
    assert "G-B3: src/secret.txt is forbidden for this task" in out.violations
    await f.ce.answer_question(q.id, "", "miles", "approve")
    out = await f.implement(files=files(f, **{"config/app.ini": "x\n", "src/secret.txt": "y\n"}))
    assert out.accepted, out.violations


@scenario()
async def test_the_oracles_tests_are_never_granted(f: Flow):
    await f.to_build(signals(scope_breadth=2, novelty_uncertainty=2, verification_quality=2))
    with pytest.raises(WorkflowError, match="only the oracle changes them"):
        await ask(f, ["tests/acceptance/test_greet.sh"])


@scenario()
async def test_a_request_for_a_file_a_parallel_build_owns_is_flagged(f: Flow):
    other = {**TASKS[0], "id": "T2", "title": "farewell", "files_modified": ["src/bye.txt"],
             "verification": ["grep -q bye src/bye.txt"]}
    await f.to_build(tasks=[TASKS[0], other])
    q = await ask(f, ["src/bye.txt"])
    assert any(w.startswith("T2 (working) also owns src/bye.txt") for w in q.warnings), q.warnings


@scenario()
async def test_over_a2a_the_build_asks_the_orchestrator_and_gets_the_decision(f: Flow):
    await f.to_build()
    impl = f.run("T1", "implementer")
    async with client(f) as c:
        res = await rpc(c, "orchestrator", send(f"/request_scope src/routes.txt: {WHY}"),
                        token(f, impl))
        tid = res["task"]["id"]
        q = f.ce.change.questions[tid]
        assert (q.kind, q.paths, q.question) == ("scope", ["src/routes.txt"], WHY)
        await f.ce.answer_question(tid, "go ahead", "miles", "approve")
        t = await task_when(c, "orchestrator", tid, token(f, impl))
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "approved by miles: you may change src/routes.txt" in str(t)
    async with client(f) as c:                            # no paths, no reason: refused at once
        res = await rpc(c, "orchestrator", send("/request_scope : "), token(f, impl))
        t = await task_when(c, "orchestrator", res["task"]["id"], token(f, impl))
        assert t["status"]["state"] == "TASK_STATE_REJECTED"
