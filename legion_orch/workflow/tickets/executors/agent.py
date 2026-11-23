"""``agent:<role>`` (and the resolver): a run of the role, through the agent runner.

The runner places the run, prepares its working copy (the ticket's branch) and runs its turns;
a rework continues the same session (R6), and a criterion's judge is a fresh agent each attempt
(its ticket is new). The run ends in an accepted report, or a failure the runner attributes to
its source (``FailureSource``) – which decides the outcome's kind:

* an accepted report: the work is done (its commit) – a judge's verdict, for a criterion; a
  ticket's own work that judges (the integration verifier) and says FAIL has not done it: an
  attempt, its findings the next round's input;
* the agent's own failure (it gave up, its report submissions ran out, a BLOCKED report): an
  attempt, the detail its next round's input;
* the system's or the controller's: a fault, retried with backoff (never an attempt);
* a timeout: its budget is exceeded – escalated at once."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ...model import FailureSource, Verdict
from .base import Backoff, Blocked, Done, Fault, GaveUp, Judged, Outcome, OverBudget, Work, attempt


@dataclass(frozen=True)
class AgentEnd:
    """How an agent's run of a ticket ended, as the agent runner tells it."""
    accepted: bool                              # its report was accepted
    sha: str | None = None                      # the commit the report attests
    verdict: Verdict | None = None              # a judge's (criteria)
    findings: tuple[str, ...] = ()              # a judge's blocking findings; a failure's detail
    failure: FailureSource | None = None        # who a failed run's failure came from
    detail: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)   # the report's named documents
    blocked: bool = False                       # its report said BLOCKED (``detail``: why)


class AgentPort(Protocol):
    """The agent runner, for tickets: one run of ``role`` on the ticket's work."""

    async def run(self, role: str, work: Work) -> AgentEnd: ...

    async def stop(self, ticket: str) -> None: ...


def agent_outcome(end: AgentEnd, criterion: bool) -> Outcome:
    if end.blocked:
        return Blocked(end.detail, end.findings)
    if end.accepted:
        if criterion:
            return Judged(end.verdict == Verdict.PASS, end.findings)
        if end.verdict is not None and end.verdict != Verdict.PASS:
            return GaveUp(end.findings or (f"its own verdict: {end.verdict}",))
        return Done(end.sha, dict(end.outputs))
    match end.failure:
        case FailureSource.TIMEOUT:
            return OverBudget(end.detail or "the run's time budget is used up")
        case FailureSource.SYSTEM | FailureSource.CONTROLLER:
            return Fault(end.detail or f"the run failed ({end.failure})")
        case _:                                 # the agent's own: an attempt
            return GaveUp(end.findings or ((end.detail,) if end.detail else ()))


class AgentExecutor:
    """Runs ``agent:<role>`` tickets – and, given ``role``, tickets of one role whatever their
    type says (the resolver)."""

    def __init__(self, port: AgentPort, backoff: Backoff = Backoff(), role: str | None = None):
        self.port, self.backoff, self.role = port, backoff, role

    async def run(self, work: Work) -> Outcome:
        role = self.role or work.kind
        criterion = work.ticket.role == "criterion"

        async def once() -> Outcome:
            return agent_outcome(await self.port.run(role, work), criterion)
        return await attempt(once, self.backoff)

    async def stop(self, ticket: str) -> None:
        await self.port.stop(ticket)
