"""Cleaning up after a change (WORKFLOW.md §5.14): a run branch goes when its node merges, Restart
plan removes the change's run branches, an ended change (its retrospective done) loses its
leftover run branches and environment images once, and a human's prune removes the integration
branch and the evidence refs."""
from __future__ import annotations

import pytest
from engine_harness import Flow, scenario

from legion_orch.workflow.engine import WorkflowError, cleanup
from legion_orch.workflow.model import ChangeStatus, Node


async def heads(f: Flow) -> list[str]:
    out = await f.ctl.git.git("for-each-ref", "--format=%(refname)", f"refs/heads/legion/{f.ce.cid}/")
    return out.split()


async def evidence(f: Flow) -> list[str]:
    return (await f.ctl.git.git("for-each-ref", "--format=%(refname)", "refs/legion/evidence/")).split()


@scenario()
async def test_a_run_branch_goes_when_its_node_merges(f: Flow):
    await f.to_build()
    await f.implement()
    branch = f"refs/heads/legion/{f.ce.cid}/build-t1"
    assert branch in await heads(f)                       # pushed, under review
    await f.review()
    node = f.ce.nodes["build:T1"]
    assert node.status == "ACCEPTED" and branch not in await heads(f)
    # the commit stays reachable: in the integration history, and pinned as evidence
    assert await f.ctl.git.is_ancestor(node.accepted_sha, f.ce.change.integration_head)
    run = f.runs("build:T1", "implementer")[0]
    assert f"refs/legion/evidence/{run.id}" in await evidence(f)


@scenario()
async def test_the_oracle_branch_goes_when_it_merges(f: Flow):
    await f.to_h2(tier="R2")
    assert f.status("oracle") == "ACCEPTED"
    assert not [h for h in await heads(f) if "oracle" in h]


@scenario()
async def test_restart_plan_removes_the_changes_run_branches(f: Flow):
    await f.to_h2(tier="R2")
    stray = f"refs/heads/legion/{f.ce.cid}/build-t1"     # e.g. pushed by an earlier plan's T1
    await f.ctl.git.git("update-ref", stray, f.ce.change.base_sha)
    await f.human("H-2", "revise", feedback="start over with smaller tasks")
    assert stray not in await heads(f)
    assert f"refs/heads/legion/{f.ce.cid}/integration" in await heads(f)


@scenario()
async def test_an_ended_change_is_cleaned_up_once(f: Flow):
    await f.to_build()
    stray = f"refs/heads/legion/{f.ce.cid}/build-t9"      # a task that never merged
    await f.ctl.git.git("update-ref", stray, f.ce.change.base_sha)
    await f.implement()
    await f.review()                                       # R1: ships, the change ends
    assert f.ce.change.status == ChangeStatus.READY
    ch = f.ce.change
    assert ch.cleaned_at and f.builder.removed == [f.ce.cid]
    assert await heads(f) == [f"refs/heads/legion/{f.ce.cid}/integration"]   # the deliverable
    assert await evidence(f)                                                    # kept
    await f.settle()
    assert f.builder.removed == [f.ce.cid]                 # once
    assert any("environment images" in e["message"] for e in f.ce.timeline_events())


@scenario()
async def test_cleanup_waits_for_the_retrospective(f: Flow):
    await f.to_h2()                                        # nothing running
    async with f.ce.lock:
        f.ce.change.status = ChangeStatus.ABANDONED
        f.ce.add_node(Node(id="retrospective", kind="retrospective", title="r", status="RUNNING"))
        assert not cleanup.due(f.ce)                       # the retrospective is still running
        f.ce.nodes["retrospective"].status = "ACCEPTED"
        assert cleanup.due(f.ce)
        f.ce.change.cleaned_at = "2026-11-19T00:00:00.000Z"
        assert not cleanup.due(f.ce)


@scenario()
async def test_prune_removes_the_integration_branch_and_evidence(f: Flow):
    await f.to_build()
    with pytest.raises(WorkflowError, match="still active"):
        await f.change_action("prune")
    await f.implement()
    await f.review()
    assert f.ce.change.status == ChangeStatus.READY and await evidence(f)
    res = await f.change_action("prune")
    assert "integration" in res.note and "no longer resolve" in res.note
    assert await heads(f) == [] and await evidence(f) == []
    assert f.ce.change.pruned_at
    with pytest.raises(WorkflowError, match="already pruned"):
        await f.change_action("prune")
    with pytest.raises(WorkflowError, match="not available"):
        await f.human("ship", "recheck")                   # nothing left to evaluate at
