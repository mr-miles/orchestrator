"""``human:signoff``: a decision owner signs off what the ticket names (its ``signs``).

A sign-off names what it signs: documents at versions (``path@version``; a bare ``path`` is its
version when the sign-off is asked for). The human is asked about those exact versions; if one
changes before they decide, the decision is about something that no longer is, so they are
asked again about the new versions. An approval passes the criterion, a rejection fails it with
the reason (a rejection at a sign-off escalates like any failure – its severity says how)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .base import Backoff, Done, Fault, GaveUp, Judged, Outcome, Work, attempt


@dataclass(frozen=True)
class Decision:
    approved: bool
    reason: str = ""


class SignoffPort(Protocol):
    """Human sign-offs: documents' current versions, and a decision owner's decision."""

    async def versions(self, paths: list[str]) -> dict[str, str | None]: ...

    async def decide(self, work: Work, subjects: Mapping[str, str]) -> Decision: ...

    async def stop(self, ticket: str) -> None: ...


def subjects_of(signs: list[str]) -> dict[str, str | None]:
    """``path@version`` (or a bare path: its current version) for each subject."""
    out: dict[str, str | None] = {}
    for s in signs:
        path, at, version = s.rpartition("@")
        out[path if at else version] = version if at else None
    return out


class SignoffExecutor:
    def __init__(self, port: SignoffPort, backoff: Backoff = Backoff(), asks: int = 5):
        self.port, self.backoff, self.asks = port, backoff, asks

    async def run(self, work: Work) -> Outcome:
        return await attempt(lambda: self._once(work), self.backoff)

    async def _once(self, work: Work) -> Outcome:
        wanted = subjects_of(work.ticket.signs)
        for _ in range(self.asks):
            current = await self.port.versions(list(wanted))
            if missing := sorted(p for p, v in current.items() if v is None):
                return Fault(f"nothing to sign off: no {', '.join(missing)}", transient=False)
            if stale := sorted(p for p, v in wanted.items() if v and current[p] != v):
                return Fault(f"the sign-off names versions that are no longer current: "
                             f"{', '.join(f'{p}@{wanted[p]} (now @{current[p]})' for p in stale)}",
                             transient=False)
            subjects = {p: v for p, v in current.items() if v is not None}
            decision = await self.port.decide(work, subjects)
            if await self.port.versions(list(wanted)) != current:
                continue                         # changed while they decided: ask again
            if work.ticket.role == "criterion":
                return Judged(decision.approved, () if decision.approved else (decision.reason,))
            return Done() if decision.approved else GaveUp((decision.reason,))
        return Fault(f"the subjects kept changing while the sign-off was asked {self.asks} times",
                     transient=False)

    async def stop(self, ticket: str) -> None:
        await self.port.stop(ticket)
