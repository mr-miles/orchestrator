"""``controller:sync``: the change's base – its workspace's hub brought up to date with its
source, the base branch's commit taken (WORKFLOW.md §5.0a). The root's first subtask: nothing
has a branch before the change has a base. A hub that cannot follow its source (rewritten,
another origin) waits on a human at once; an outage is retried with backoff. Run again (a
retry, a restart), a change that has its base keeps it."""
from __future__ import annotations

from typing import Protocol

from .base import Backoff, Done, Fault, Outcome, Work, attempt


class SyncPort(Protocol):
    """The workspace, for tickets: None when the change has its base, else why the hub cannot
    follow its source (raises on an outage)."""

    async def sync(self, work: Work) -> str | None: ...


class SyncExecutor:
    def __init__(self, port: SyncPort, backoff: Backoff = Backoff()):
        self.port, self.backoff = port, backoff

    async def run(self, work: Work) -> Outcome:
        async def once() -> Outcome:
            unsynced = await self.port.sync(work)
            return Done() if unsynced is None else Fault(unsynced, transient=False)
        return await attempt(once, self.backoff)

    async def stop(self, ticket: str) -> None:
        return None                               # a fetch: it completes or fails by itself
