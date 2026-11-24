"""Cleaning up after a change (WORKFLOW.md §5.14).

* A ticket's branch goes when its commit is merged: it is part of its target's history now, and
  the evidence ref (``refs/legion/evidence/<run>``) keeps every commit judged.
* When the change has ended and its retrospective is done, the branches left (tickets that never
  merged) and the environment images built for it on this host are removed – once.
* ``prune`` (a human, after taking the integration branch) removes the integration branch and the
  change's evidence refs: the change's records stay, its ``git:`` attestations stop resolving.
"""
from __future__ import annotations

from ....util import now_iso
from ...model import LIVE_RUN, ChangeStatus
from ..base.common import WorkflowError
from ..base.core import ChangeCore


async def delete_run_branch(c: ChangeCore, branch: str | None, ticket_id: str) -> None:
    """A merged ticket's branch: its commit is in its target's history now."""
    if branch and await c.git.delete_ref(f"refs/heads/{branch}"):
        async with c.lock:
            c.event("cleanup", f"deleted branch {branch} (merged)", ticket_id)


async def delete_run_branches(c: ChangeCore, why: str) -> list[str]:
    gone = []
    for b in await c.git.run_branches(c.cid):
        if await c.git.delete_ref(f"refs/heads/{b}"):
            gone.append(b)
    if gone:
        c.event("cleanup", f"deleted {len(gone)} run branch(es) ({why}): {', '.join(gone)}")
    return gone


def due(c: ChangeCore) -> bool:
    """The change has ended, nothing of it is running, and its on-complete process (the
    retrospective) is done."""
    ch = c.change
    if ch is None or ch.status == ChangeStatus.ACTIVE or ch.cleaned_at or not ch.completed:
        return False
    return not any(r.status in LIVE_RUN for r in c.runs.values())


async def end_of_change(c: ChangeCore) -> None:
    """Remove the ended change's leftover run branches and its environment images (once)."""
    async with c.lock:
        if not due(c):
            return
        c.change.cleaned_at = now_iso()       # once, even if a step below fails
        c.save()
    await delete_run_branches(c, "the change ended")
    log = await c.svc.builder.remove_change_images(c.cid)
    async with c.lock:                # later runs use the default image (sandbox_env)
        c.event("cleanup", f"environment images: {log}")
        c.save()


async def prune(c: ChangeCore, approver: str) -> str:
    """A human's last step for an ended change: the integration branch and the evidence refs go.
    The caller holds the lock."""
    ch = c.change
    if ch.status == ChangeStatus.ACTIVE:
        raise WorkflowError("prune is for an ended change (this one is still active)")
    if ch.pruned_at:
        raise WorkflowError(f"already pruned at {ch.pruned_at}")
    gone = await delete_run_branches(c, "pruned")
    branch = c.git.integration_branch(ch.id)
    if await c.git.delete_ref(f"refs/heads/{branch}"):
        gone.append(branch)
    evidence = [r for r in c.runs if await c.git.delete_ref(f"refs/legion/evidence/{r}")]
    ch.pruned_at = now_iso()
    c.event("cleanup", f"{approver} pruned the change: {', '.join(gone) or 'no branches'}; "
            f"{len(evidence)} evidence ref(s)")
    return (f"removed {', '.join(gone) or 'no branches'} and {len(evidence)} evidence ref(s); "
            "the change's records stay, its git: attestations no longer resolve")
