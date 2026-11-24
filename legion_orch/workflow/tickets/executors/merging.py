"""``controller:can-merge``, and the merge of a passed ticket (not a ticket: a rule, R5, R8).

Branches follow the tree (:mod:`legion_orch.git.branches`): a ticket's commit merges into its
parent's branch. "Can merge" checks that it would, now, under the branch's lock; the merge, when
the ticket has passed its criteria, checks again and merges under the same lock – a sibling
that merged in between can make it conflict, which is a failed "can merge" (rework: rebase onto
the new head). Git errors are faults, retried with backoff."""
from __future__ import annotations

from ....git.branches import Branches, MergeCheck
from ..rules import Effect, Tickets
from .base import Backoff, Fault, Judged, Outcome, Work, attempt


class CanMergeExecutor:
    def __init__(self, branches: Branches, backoff: Backoff = Backoff()):
        self.branches, self.backoff = branches, backoff

    async def run(self, work: Work) -> Outcome:
        if work.judged is None or work.into is None:
            return Fault("nothing to merge: no commit, or no branch to merge it into",
                         transient=False)
        sha, into = work.judged, work.into

        async def once() -> Outcome:
            check = await self.branches.can_merge(sha, into)
            return Judged(True) if check.clean else Judged(False, (check.finding(into),))
        return await attempt(once, self.backoff)

    async def stop(self, ticket: str) -> None:
        return None                               # a check under a lock: over in a moment


class Merger:
    """Carries out the rules' ``Merge`` effects: ``into`` is the branch of the ticket the commit
    merges into (the runner's mapping from ticket to branch)."""

    def __init__(self, branches: Branches, backoff: Backoff = Backoff()):
        self.branches, self.backoff = branches, backoff

    async def merge(self, sha: str, into: str, message: str) -> MergeCheck | Fault:
        return await attempt(lambda: self.branches.merge(sha, into, message), self.backoff)


def merge_outcome(tickets: Tickets, tid: str, into: str, done: MergeCheck | Fault) -> list[Effect]:
    """The merge's result as the rules' event: merged (the parent's new head), a conflict (a
    rework, G-A2), or a fault that outlasted its backoff."""
    if isinstance(done, Fault):
        return tickets.system_error(tid)
    if done.clean:
        return tickets.merged(tid, done.head)
    return tickets.merge_conflict(tid, [done.finding(into)])
