"""What every executor shares (plan: ~/plans/tickets-design.md §3): the work it is given, the one
outcome it ends in, how that outcome becomes a rules event, and the backoff for system errors.

An executor does a ticket's own work – an agent's run, a sign-off, a verification, a controller
action – and says how it went, in one of four kinds (~/plans/tickets.md, "Errors by kind"):

* :class:`Done` – the work is finished (an accepted report, its commit); for a criterion,
  :class:`Judged` – its verdict and findings;
* :class:`GaveUp` – an agent outcome without an acceptable result (it gave up, its report
  submissions ran out): an attempt;
* :class:`Fault` – a system error (sandbox, gateway, git, a verifier's infrastructure): retried
  with backoff (:func:`attempt`), then the ticket waits on a human – never an attempt;
* :class:`OverBudget` – its budget is exceeded: the ticket waits at once (no retry, no attempt).

Executors raise nothing for these: an unexpected exception is a :class:`Fault` too
(:func:`attempt` turns it into one), so a bug in an executor waits on a human rather than
counting against an agent."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from ..model import GROUPING, Ticket
from ..rules import Effect, Tickets

log = logging.getLogger(__name__)
T = TypeVar("T")


# ------------------------------------------------------------------------------- the work
@dataclass(frozen=True)
class Work:
    """One round of a ticket's own work, as the runner hands it to the ticket's executor.

    ``branch``: the ticket's own branch (code tickets); ``base``: its parent's branch head when it
    started – where its branch starts, and (publish) the result it sends on. For a criterion,
    ``judged`` is the commit it judges (its owner's) and ``into`` the branch that commit merges
    into (the owner's parent's)."""
    ticket: Ticket
    attempt: int
    findings: tuple[str, ...] = ()
    change: str = ""
    branch: str | None = None
    base: str | None = None
    judged: str | None = None
    into: str | None = None
    labels: frozenset[str] = frozenset()       # its effective risk labels (inherited)
    subjects: dict[str, Any] | None = None     # (sign-offs) what it is asked to approve: each
                                               # output's pinned documents, ``head`` the commit
    inputs: dict[str, Any] = field(default_factory=dict)   # its inputs: the approved versions

    @property
    def kind(self) -> str:
        """Its type's parameter: the role of ``agent:<role>``, the action of
        ``controller:<action>``."""
        return self.ticket.type.partition(":")[2]


# ------------------------------------------------------------------------------- outcomes
@dataclass(frozen=True)
class Done:
    sha: str | None = None                     # the commit its accepted report attests
    outputs: dict[str, Any] = field(default_factory=dict)  # name -> the documents it names


@dataclass(frozen=True)
class Judged:
    passed: bool
    findings: tuple[str, ...] = ()
    signed: dict[str, Any] | None = None       # (sign-offs) exactly what was approved


@dataclass(frozen=True)
class GaveUp:
    findings: tuple[str, ...] = ()             # what the next attempt is told


@dataclass(frozen=True)
class Fault:
    detail: str
    transient: bool = True                     # False: retrying cannot help (a human fixes it)


@dataclass(frozen=True)
class OverBudget:
    detail: str


Outcome = Done | Judged | GaveUp | Fault | OverBudget


def apply(tickets: Tickets, tid: str, outcome: Outcome) -> list[Effect]:
    """The outcome of a ticket's work as its rules event. A criterion's work ends in a verdict;
    a :class:`Done` from a criterion's executor is a pass, a :class:`Judged` from a ticket's a
    fault (an executor's bug, not the agent's)."""
    criterion = tickets.t[tid].role == "criterion"
    match outcome:
        case Judged(passed, findings, signed) if criterion:
            return tickets.verdict(tid, passed, findings, signed=signed)
        case Done() if criterion:
            return tickets.verdict(tid, True)
        case Done(sha, outputs):
            return tickets.work_done(tid, sha, outputs)
        case GaveUp(findings):
            return tickets.work_failed(tid, findings)
        case OverBudget():
            return tickets.budget_exceeded(tid)
        case _:
            return tickets.system_error(tid)


# ------------------------------------------------------------------------------- executors
class Executor(Protocol):
    """Does a ticket's own work. ``stop``: the work is no longer wanted (abandon); it ends
    as soon as it can (its outcome is then ignored)."""

    async def run(self, work: Work) -> Outcome: ...

    async def stop(self, ticket: str) -> None: ...


class Executors:
    """The executor of each ticket type: the type's own (``controller:verify``), else its family's
    (``agent`` for ``agent:<role>``). Grouping tickets have none: the runner runs them."""

    def __init__(self, by_type: Mapping[str, Executor]):
        self.by_type = dict(by_type)

    def __call__(self, type_: str) -> Executor:
        if type_ == GROUPING:
            raise KeyError("a grouping ticket has no executor")
        found = self.by_type.get(type_) or self.by_type.get(type_.partition(":")[0])
        if found is None:
            raise KeyError(f"no executor for ticket type {type_!r}")
        return found

    def types(self) -> set[str]:
        return set(self.by_type)


# ------------------------------------------------------------------------------- backoff
@dataclass(frozen=True)
class Backoff:
    """How system errors are retried: ``tries`` in all, waiting ``first`` seconds after the first
    failure, then ``factor`` times longer each time, at most ``cap``."""
    tries: int = 4
    first: float = 2.0
    factor: float = 2.0
    cap: float = 60.0

    def delays(self) -> Iterable[float]:
        d = self.first
        for _ in range(self.tries - 1):
            yield min(d, self.cap)
            d *= self.factor


@dataclass
class Attempts:
    """The faults a ticket's work met on the way to its outcome (shown with an escalation)."""
    faults: list[str] = field(default_factory=list)


async def attempt(work: Callable[[], Awaitable[T | Fault]], backoff: Backoff = Backoff(), *,
                  sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                  seen: Attempts | None = None) -> T | Fault:
    """``work``'s outcome, its transient faults retried with ``backoff``: the last fault once
    the tries are used up. An exception is a fault (logged: it is a bug or an outage); a
    cancellation is not caught."""
    delays = iter(backoff.delays())
    while True:
        try:
            out: T | Fault = await work()
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001 – a fault, not a crash
            log.exception("ticket work failed")
            out = Fault(f"{type(e).__name__}: {e}")
        if not isinstance(out, Fault):
            return out
        if seen is not None:
            seen.faults.append(out.detail)
        delay = next(delays, None) if out.transient else None
        if delay is None:
            return out
        await sleep(delay)
