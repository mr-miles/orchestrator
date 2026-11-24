"""A human's ``restart`` of an escalated build: its branch and sandboxes are deleted first (a
failure keeps it waiting), then it starts afresh at attempt 1; run records and evidence stay."""
from __future__ import annotations

import pytest
from engine_harness import Flow, scenario

from legion_orch.workflow.engine import WorkflowError


async def escalate(f: Flow) -> None:
    """T1's review fails with the same finding twice: it waits on a human."""
    await f.to_build()
    await f.implement()
    await f.review(verdict="FAIL")
    await f.implement()
    await f.review(verdict="FAIL")
    assert (f.ticket("T1").status, f.ticket("T1").waiting) == ("waiting", "repeated"), f.status()


async def branch(f: Flow, name: str) -> str | None:
    out = await f.git.git("for-each-ref", "--format=%(objectname)", f"refs/heads/{name}")
    return out.strip() or None


@scenario()
async def test_a_restart_deletes_the_branch_and_sandboxes_and_starts_afresh(f: Flow):
    await escalate(f)
    name = f"legion/{f.ce.cid}/t/T1"
    old = await branch(f, name)
    assert old
    runs = f.runs("T1")
    sandboxes = {r.sandbox for r in runs if r.sandbox and r.sandbox_ref}
    assert sandboxes
    await f.human("T1", "restart")
    t = f.ticket("T1")
    assert (t.status, t.attempt) == ("working", 1) and not t.findings
    assert sandboxes <= set(f.runner.deleted)
    new = await branch(f, name)
    parent = await branch(f, f"legion/{f.ce.cid}/t/builds") or await branch(
        f, f"legion/{f.ce.cid}/t/delivery") or await branch(f, f"legion/{f.ce.cid}/integration")
    assert new and new != old and new == parent
    assert not await f.git.is_ancestor(old, new)
    ev = (await f.git.git("for-each-ref", "--format=%(refname)", "refs/legion/evidence/")).split()
    assert all(f"refs/legion/evidence/{r.id}" in ev for r in runs if r.role == "implementer")
    assert all(r.id in f.ce.runs for r in runs)                    # earlier runs remain
    msgs = [e["message"] for e in f.ce.timeline_events()]
    assert any("restart" in m for m in msgs)
    assert any("deleted sandbox" in m for m in msgs)
    assert any("deleted branch" in m for m in msgs)
    run = f.run("T1", "implementer")                                # a fresh run, no findings
    assert "missing newline handling" not in f.runner.payloads[run.sandbox]["prompt.md"]


@scenario()
async def test_only_a_decision_owner_may_restart(f: Flow):
    await escalate(f)
    gone = list(f.runner.deleted)
    with pytest.raises(WorkflowError, match="not a decision owner"):
        await f.human("T1", "restart", approver="mallory")
    assert f.ticket("T1").status == "waiting" and f.runner.deleted == gone
    assert await branch(f, f"legion/{f.ce.cid}/t/T1")


@scenario()
async def test_a_grouping_ticket_cannot_restart(f: Flow):
    await f.to_build()
    for task in [t for t, x in f.ce.st.tickets.items() if x.type == "agent:implementer"]:
        await f.implement(task)
        if f"review:{task}" in f.ce.st.tickets:
            await f.review(task)
    await f.integration("FAIL")
    assert f.ticket("delivery").waiting == "escalated"
    gone = list(f.runner.deleted)
    with pytest.raises(WorkflowError):
        await f.human("delivery", "restart")
    assert f.ticket("delivery").status == "waiting" and f.runner.deleted == gone


@scenario()
async def test_a_failed_cleanup_keeps_it_waiting_and_a_second_restart_succeeds(f: Flow):
    await escalate(f)
    f.runner.fail_delete = True
    with pytest.raises(WorkflowError):
        await f.human("T1", "restart")
    t = f.ticket("T1")
    assert (t.status, t.attempt) == ("waiting", 2) and t.runs
    assert any("cannot delete sandbox" in e["message"] for e in f.ce.timeline_events())
    f.runner.fail_delete = False
    await f.human("T1", "restart")
    assert (f.ticket("T1").status, f.ticket("T1").attempt) == ("working", 1)
