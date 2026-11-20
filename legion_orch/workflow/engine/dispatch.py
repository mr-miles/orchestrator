"""The dispatcher: runs work within the sandbox limit (plan: state-model.md, addendum).

``limits.max_parallel`` bounds the work running at once across every change – an agent's run, a
verifier sandbox. The dispatcher is given work it does not look into: anything with an
``async execute()``. It runs each piece when a slot is free and returns its result; it knows nothing
of agents, verifications, nodes or steps (a :class:`.work.Work` is a sandbox session and its
commands; the dispatcher only calls its ``execute``).

**Reservations.** A step holds one, so the work of its phases – its agent's run, then its
verification, then its judges – follows on in the same slot instead of queueing behind other
steps' new work. A reservation takes a real slot only when its first work runs, and keeps it
between pieces of work that follow straight on. While its step waits on its conditions (a
rework waiting for the environment, say) it gives the slot back (:meth:`Dispatcher.idle`): a
waiting step holds no slot, so it cannot starve the work it waits for. Its next work binds a
slot again. It is released when the step is done, sent back or escalated. Work
dispatched under a reservation while its slot is busy (a panel's judges, in parallel) waits for
a free slot that nobody holds, or for the reserved one as the work in it completes – whichever
comes first. Waiting work is served first come, first served.
"""
from __future__ import annotations

import asyncio
import itertools
from typing import Protocol, TypeVar

T = TypeVar("T")


class Executable(Protocol[T]):
    """What the dispatcher runs (it calls nothing else on it)."""

    async def execute(self) -> T: ...


class Withdrawn(Exception):
    """The work's reservation was released before the work got a slot: it never ran."""


class Reservation:
    """A step's claim on one slot, from its first work until :meth:`Dispatcher.release`."""

    def __init__(self, dispatcher: Dispatcher):
        self._dispatcher = dispatcher
        self.bound = False          # it holds a slot (taken by its first work)
        self.busy = False           # work runs in its slot
        self.released = False

    def release(self) -> None:
        self._dispatcher.release(self)


class Dispatcher:
    def __init__(self, limit: int):
        self.limit = limit
        self.in_use = 0             # slots taken: work in unreserved slots, bound reservations
        self._waiting: list[tuple[int, Reservation | None, asyncio.Future[bool]]] = []
        self._order = itertools.count()

    @property
    def waiting(self) -> int:
        """Work waiting for a slot."""
        return sum(1 for *_, f in self._waiting if not f.done())

    def reserve(self) -> Reservation:
        """A reservation; it takes no slot until its first work runs."""
        return Reservation(self)

    async def run(self, work: Executable[T], reservation: Reservation | None = None) -> T:
        """Run ``work`` once there is a slot for it, and return its result. Raises
        :class:`Withdrawn` if its reservation is released while it waits."""
        if reservation is not None and reservation.released:
            raise Withdrawn()
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._waiting.append((next(self._order), reservation, fut))
        self._grant()
        try:
            reserved = await asyncio.shield(fut)
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():         # granted as it was cancelled
                self._done(reservation, fut.result())
            else:
                fut.cancel()
                self._prune()
            raise
        try:
            return await work.execute()
        finally:
            self._done(reservation, reserved)

    def idle(self, reservation: Reservation) -> None:
        """The reservation's step waits on its conditions: its idle slot goes back (it stays
        reserved; its next work binds a slot again)."""
        if reservation.bound and not reservation.busy:
            reservation.bound = False
            self.in_use -= 1
            self._grant()

    def release(self, reservation: Reservation) -> None:
        """The step needs its slot no more: work still waiting under it is withdrawn, and the
        slot goes back once the work in it (if any) completes."""
        if reservation.released:
            return
        reservation.released = True
        for _, r, fut in self._waiting:
            if r is reservation and not fut.done():
                fut.set_exception(Withdrawn())
        self._prune()
        if reservation.bound and not reservation.busy:
            reservation.bound = False
            self.in_use -= 1
        self._grant()

    # ------------------------------------------------------------------ slots
    def _grant(self) -> None:
        """Serve the waiting work in order: into its idle reserved slot, else a free one (which
        a reservation without a slot then keeps)."""
        for _, r, fut in self._waiting:
            if fut.done():
                continue
            if r is not None and r.bound and not r.busy:
                r.busy = True
                fut.set_result(True)
            elif self.in_use < self.limit:
                self.in_use += 1
                if r is not None and not r.bound:
                    r.bound = r.busy = True
                    fut.set_result(True)
                else:
                    fut.set_result(False)
        self._prune()

    def _done(self, reservation: Reservation | None, reserved: bool) -> None:
        if reserved:
            assert reservation is not None
            reservation.busy = False
            if reservation.released:
                reservation.bound = False
                self.in_use -= 1
        else:
            self.in_use -= 1
        self._grant()

    def _prune(self) -> None:
        self._waiting = [w for w in self._waiting if not w[2].done()]


class StepDispatcher:
    """What a step's phases dispatch through: the dispatcher, with the step's reservation."""

    def __init__(self, dispatcher: Dispatcher, reservation: Reservation):
        self._dispatcher, self.reservation = dispatcher, reservation

    @property
    def released(self) -> bool:
        """The step is over (done, sent back, escalated): its work's results count no more."""
        return self.reservation.released

    async def run(self, work: Executable[T]) -> T:
        return await self._dispatcher.run(work, self.reservation)

    def idle(self) -> None:
        """The step waits on its conditions: it holds no slot meanwhile."""
        self._dispatcher.idle(self.reservation)


__all__ = ["Dispatcher", "Executable", "Reservation", "StepDispatcher", "Withdrawn"]
