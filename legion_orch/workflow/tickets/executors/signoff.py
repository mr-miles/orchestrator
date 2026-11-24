"""``human:signoff``: a decision owner signs off the outputs the ticket names (its ``signs``).

The runner resolves them (:mod:`..outputs`) to exactly what the work it checks produced – each
output's documents as its accepted report pinned them, ``head`` the judged commit – and hands
them over as ``Work.subjects``. Pinned outputs cannot change (an agent cannot write once its
report is accepted), so the human decides about exactly these; an approval passes the criterion
and carries them as ``signed`` – the versions the change's pointers move to –, a rejection fails
it with the reason (its severity says how). Subjects that cannot be resolved are a fault a human
fixes (the plan names an output nobody produced)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .base import Backoff, Done, Fault, GaveUp, Judged, Outcome, Work, attempt


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str = ""


class SignoffPort(Protocol):
    """Human sign-offs: a decision owner's decision about ``subjects``."""

    async def decide(self, work: Work, subjects: Mapping[str, Any]) -> Decision: ...

    async def stop(self, ticket: str) -> None: ...


class SignoffExecutor:
    def __init__(self, port: SignoffPort, backoff: Backoff = Backoff()):
        self.port, self.backoff = port, backoff

    async def run(self, work: Work) -> Outcome:
        if not work.subjects:
            return Fault("nothing to sign off: its subjects could not be resolved",
                         transient=False)
        return await attempt(lambda: self._once(work), self.backoff)

    async def _once(self, work: Work) -> Outcome:
        subjects = dict(work.subjects or {})
        decision = await self.port.decide(work, subjects)
        if work.ticket.role == "criterion":
            if decision.approved:
                return Judged(True, signed=subjects)
            return Judged(False, (decision.reason,))
        return Done() if decision.approved else GaveUp((decision.reason,))

    async def stop(self, ticket: str) -> None:
        await self.port.stop(ticket)
