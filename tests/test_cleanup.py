"""Cleaning up after a change (WORKFLOW.md §5.14): a ticket's branch goes once it has merged, every
commit an agent reported is pinned as evidence, an ended change (its on-complete work done)
loses its leftover branches and environment images once, and a human's prune removes the
integration branch and the evidence refs."""
from __future__ import annotations

import pytest
from engine_harness import Flow, scenario

from legion_orch.workflow.engine import WorkflowError
from legion_orch.workflow.engine.services import cleanup
from legion_orch.workflow.model import ChangeStatus


async def heads(f: Flow) -> list[str]:
    out = await f.git.git("for-each-ref", "--format=%(refname)", f"refs/heads/legion/{f.ce.cid}/")
    return out.split()


async def evidence(f: Flow) -> list[str]:
    return (await f.git.git("for-each-ref", "--format=%(refname)", "refs/legion/evidence/")).split()


@scenario()
async def test_a_tickets_branch_goes_once_it_has_merged(f: Flow):
    await f.to_build()
    await f.implement()
    branch = f"refs/heads/legion/{f.ce.cid}/t/T1"
    assert branch in await heads(f)                       # under review
    await f.review()
    await f.settle()
    t = f.ticket("T1")
    assert t.status == "passed", f.status()
    assert branch not in await heads(f), await heads(f)
    # the commit stays reachable: in the integration history, and pinned as evidence
    head = (await f.git.git("rev-parse", f"refs/heads/legion/{f.ce.cid}/integration")).strip()
    assert await f.git.is_ancestor(t.sha, head), (t.sha, head)
    run = f.runs("T1", "implementer")[0]
    assert f"refs/legion/evidence/{run.id}" in await evidence(f)


@scenario()
async def test_an_ended_change_is_cleaned_up_once(f: Flow):
    await f.to_build()
    stray = f"refs/heads/legion/{f.ce.cid}/t/T9"          # a ticket that never merged
    await f.git.git("update-ref", stray, f.ce.change.base_sha)
    await f.to_end_from_build()
    await f.settle()
    ch = f.ce.change
    assert ch.status == ChangeStatus.READY
    assert ch.cleaned_at and f.builder.removed == [f.ce.cid]
    assert await heads(f) == [f"refs/heads/legion/{f.ce.cid}/integration"]   # the deliverable
    assert await evidence(f)                                                    # kept
    await f.settle()
    assert f.builder.removed == [f.ce.cid]                 # once
    assert any("environment images" in e["message"] for e in f.ce.timeline_events())


@scenario()
async def test_cleanup_waits_for_the_change_to_complete_and_its_runs(f: Flow):
    async with f.ce.lock:
        assert not cleanup.due(f.ce)                       # active, the intake running
        f.ce.change.status = ChangeStatus.ABANDONED
        assert not cleanup.due(f.ce)                       # not completed (its end not run)
        f.ce.change.completed = True
        assert not cleanup.due(f.ce)                       # the intake's run is still live
        f.run("intake").status = "FAILED"
        assert cleanup.due(f.ce)
        f.ce.change.cleaned_at = "2026-11-19T00:00:00.000Z"
        assert not cleanup.due(f.ce)


@scenario()
async def test_prune_removes_the_integration_branch_and_evidence(f: Flow):
    await f.to_build()
    with pytest.raises(WorkflowError, match="still active"):
        await f.change_action("prune")
    await f.to_end_from_build()
    await f.settle()
    assert f.ce.change.status == ChangeStatus.READY and await evidence(f)
    res = await f.change_action("prune")
    assert "integration" in res.note and "no longer resolve" in res.note
    assert await heads(f) == [] and await evidence(f) == []
    assert f.ce.change.pruned_at
    with pytest.raises(WorkflowError, match="already pruned"):
        await f.change_action("prune")
