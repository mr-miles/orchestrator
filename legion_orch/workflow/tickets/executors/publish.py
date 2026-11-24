"""``controller:publish``: the change's result leaves the workflow – ``release`` always publishes.

The result is the ticket's ``base``: its parent's branch head when it started (``release``'s,
which starts from the root's, so everything delivered). With a remote, it is pushed and its pull
request opened or brought up to date (the publisher – GitHub; done again, it changes only what
differs). With none (a workspace without a source repository), it is merged into the workspace's
base branch in the hub, the pull request's text the merge commit's message. A push or GitHub
failing is a fault, retried with backoff; a base branch it no longer merges into escalates."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from ....git.branches import Branches
from .base import Backoff, Done, Fault, Outcome, Work, attempt


class RemotePort(Protocol):
    """The publisher with a remote: pushes ``head`` and opens or updates its pull request (its
    URL); raises on failure."""

    async def publish(self, work: Work, head: str) -> str: ...


class PublishExecutor:
    """``remote``: None publishes into the hub's ``base_branch``; ``message``: the pull request's
    text (title, then body), the merge's message."""

    def __init__(self, *, remote: RemotePort | None, branches: Branches, base_branch: str,
                 message: Callable[[Work], Awaitable[str]], backoff: Backoff = Backoff()):
        self.remote, self.branches, self.base_branch = remote, branches, base_branch
        self.message, self.backoff = message, backoff

    async def run(self, work: Work) -> Outcome:
        head = work.base
        if head is None:
            return Fault("nothing to publish: no result", transient=False)
        return await attempt(lambda: self._once(work, head), self.backoff)

    async def _once(self, work: Work, head: str) -> Outcome:
        if self.remote is not None:
            await self.remote.publish(work, head)
            return Done()
        merged = await self.branches.merge(head, self.base_branch, await self.message(work))
        if not merged.clean:
            return Fault(merged.finding(self.base_branch), transient=False)
        return Done()

    async def stop(self, ticket: str) -> None:
        return None                               # a push: it completes or fails by itself
