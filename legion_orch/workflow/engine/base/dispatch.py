"""The dispatcher: runs work within the sandbox limit (plan: state-model.md, addendum).

``limits.max_parallel`` bounds the work running at once across every change – an agent's run, a
verifier sandbox. The dispatcher is given work it does not look into: anything that says whether
it ``can_start()`` and has an ``async execute()``. It runs each piece once a slot is free and the
work can start (its sandbox's environment is not being built again), first come, first served,
and returns its result; it knows nothing of agents, verifications or tickets (a
:class:`.work.Work` is a sandbox session and its commands). Work that cannot start yet keeps its
place and holds no slot; the dispatcher looks again whenever a slot frees and whenever a change
saves (:meth:`Dispatcher.recheck` – a build that finished lets its waiting work start).
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
from typing import Protocol, TypeVar

T = TypeVar("T")


class Executable(Protocol[T]):
    """What the dispatcher runs: given a slot once it can start (it calls nothing else)."""

    def can_start(self) -> bool: ...

    async def execute(self) -> T: ...


class Dispatcher:
    def __init__(self, limit: int):
        self.limit = limit
        self.in_use = 0             # slots taken
        self._waiting: list[tuple[asyncio.Future[None], Executable]] = []

    @property
    def waiting(self) -> int:
        """Work waiting for a slot."""
        return sum(1 for f, _ in self._waiting if not f.done())

    async def run(self, work: Executable[T]) -> T:
        """Run ``work`` once there is a slot for it and it can start; return its result."""
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiting.append((fut, work))
        self.recheck()
        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():         # granted as it was cancelled
                self._done()
            else:
                fut.cancel()
                self._prune()
            raise
        try:
            return await work.execute()
        finally:
            self._done()

    def recheck(self) -> None:
        """Serve the waiting work that can start, in order, while slots are free. Called when a
        slot frees, when work arrives and when a change saves (what it waited for may be
        ready now)."""
        for fut, work in self._waiting:
            if self.in_use >= self.limit:
                break
            if not fut.done() and work.can_start():
                self.in_use += 1
                fut.set_result(None)
        self._prune()

    def _done(self) -> None:
        self.in_use -= 1
        self.recheck()

    def _prune(self) -> None:
        self._waiting = [w for w in self._waiting if not w[0].done()]


__all__ = ["Dispatcher", "Executable", "PriorityCap"]


class PriorityCap:
    """At most ``limit`` holders at once, granted lowest priority first, then in request
    order: the verification service's limit on verifier sandboxes
    (``limits.max_parallel_verifications``), shared by every change, inside the dispatcher's."""

    def __init__(self, limit: int):
        self.limit, self.held = max(1, limit), 0
        self._heap: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()

    @property
    def waiting(self) -> int:
        return sum(1 for *_, f in self._heap if not f.done())

    async def acquire(self, priority: int) -> None:
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (priority, next(self._seq), fut))
        self._grant()
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()              # granted as it was cancelled: give it back
            raise

    def release(self) -> None:
        self.held -= 1
        self._grant()

    def _grant(self) -> None:
        while self._heap and self.held < self.limit:
            *_, fut = heapq.heappop(self._heap)
            if not fut.done():
                self.held += 1
                fut.set_result(None)
