"""The sandboxes in use, and who waits for one (plan: state-model.md, addendum).

``limits.max_parallel`` bounds the sandboxes in use at once across every change: agent runs (from
their preparation until their agent ends its turn) and verifier sandboxes. One queue, shared by
every change's :class:`.Dispatcher` and used only through it. A request takes its place in the
queue at once and is awaited; verifiers go first (they finish work in flight), then first come,
first served. A slot is returned when its sandbox is done; the next waiter gets it.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools

VERIFIER, AGENT = 0, 1          # priority: lower goes first


class Slot:
    """A sandbox's place within the limit; :meth:`release` gives it back (once)."""

    def __init__(self, queue: SandboxQueue, kind: int):
        self._queue, self.kind, self._held = queue, kind, True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._queue._release()


class SandboxQueue:
    def __init__(self, limit: int):
        self.limit = limit
        self.in_use = 0
        self._waiting: list[tuple[int, int, asyncio.Future[Slot]]] = []
        self._order = itertools.count()

    @property
    def waiting(self) -> int:
        return sum(1 for *_, f in self._waiting if not f.done())

    def request(self, kind: int) -> asyncio.Future[Slot]:
        """Take a place in the queue now; the future is a slot once it is this request's turn
        (cancel it to give the place up)."""
        fut: asyncio.Future[Slot] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiting, (kind, next(self._order), fut))
        self._grant()
        return fut

    def try_take(self, kind: int) -> Slot | None:
        """A slot now, if one is free and nobody waits ahead of ``kind``."""
        if self.in_use < self.limit and not any(
                k <= kind for k, _, f in self._waiting if not f.done()):
            self.in_use += 1
            return Slot(self, kind)
        return None

    def _grant(self) -> None:
        while self._waiting and self.in_use < self.limit:
            kind, _, fut = heapq.heappop(self._waiting)
            if fut.done():                       # given up (cancelled)
                continue
            self.in_use += 1
            fut.set_result(Slot(self, kind))

    def _release(self) -> None:
        self.in_use -= 1
        self._grant()


async def take(fut: asyncio.Future[Slot]) -> Slot:
    """Await a requested slot; if the awaiting task is cancelled after the slot was granted, the
    slot goes back."""
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        if fut.done() and not fut.cancelled():
            fut.result().release()
        else:
            fut.cancel()
        raise
