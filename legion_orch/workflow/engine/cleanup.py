"""Cleaning up after a change (WORKFLOW.md §5.14).

* A run branch goes when its node is accepted: the merge made its commit part of the integration
  history, and the evidence ref (``refs/legion/evidence/<run>``) keeps every candidate reviewed.
* Restart plan removes the change's run branches: a restarted task of the same name must not
  continue the abandoned plan's commits.
* When the change has ended and its retrospective is done, the run branches left (tasks that
  never merged) and the environment images built for it on this host are removed – once.
* ``prune`` (a human, after taking the integration branch) removes the integration branch and the
  change's evidence refs: the change's records stay, its ``git:`` attestations stop resolving.
"""
from __future__ import annotations

from ...util import now_iso
from ..model import ACCEPTED, FAILED, LIVE_RUN, RETRO, ChangeStatus
from .common import WorkflowError
from .core import ChangeCore


async def delete_run_branch(c: ChangeCore, branch: str | None, node_id: str) -> None:
    """The accepted run's branch: its commit is in the integration history now."""
    if branch and await c.git.delete_ref(f"refs/heads/{branch}"):
        c.event("cleanup", f"deleted branch {branch} (merged)", node_id)


async def delete_run_branches(c: ChangeCore, why: str) -> list[str]:
    gone = []
    for b in await c.git.run_branches(c.cid):
        if await c.git.delete_ref(f"refs/heads/{b}"):
            gone.append(b)
    if gone:
        c.event("cleanup", f"deleted {len(gone)} run branch(es) ({why}): {', '.join(gone)}")
    return gone


def due(c: ChangeCore) -> bool:
    """The change has ended, nothing of it is running, and its retrospective (if any) is done."""
    ch = c.change
    if ch is None or ch.status == ChangeStatus.ACTIVE or ch.cleaned_at:
        return False
    retro = c.nodes.get(RETRO)
    if retro is not None and retro.status not in (ACCEPTED, FAILED):
        return False
    if c.cfg.retrospective.auto and retro is None:
        return False                   # it is about to be queued
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
