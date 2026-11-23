"""What actions return to their callers: an operator's human action (the UI and CLI) and an
agent's ``report`` (the MCP tool). Failures are raised, so a result always means success,
except a report, whose outcome is either accepted or the list of violated guards."""
from __future__ import annotations

from .model import Record


class ActionResult(Record):
    """The result of a human action on a ticket or on the change."""
    status: str | None = None               # the ticket's status afterwards (ticket actions)
    note: str | None = None                 # what happened, or something the operator should know


class ReportOutcome(Record):
    """The result of an agent's report: accepted (with the node's next status) or rejected
    with the violated guards. A dry run is never accepted: ``would_accept`` says whether the
    same report would be, and ``violations`` lists what it would be rejected for."""
    accepted: bool
    violations: list[str] = []
    attempts_left: int | None = None
    next: str | None = None
    dry_run: bool | None = None
    would_accept: bool | None = None
    instruction: str | None = None          # what the agent does next (accepted: end the turn)

    @classmethod
    def reject(cls, *violations: str, attempts_left: int | None = None) -> ReportOutcome:
        return cls(accepted=False, violations=list(violations), attempts_left=attempts_left)
