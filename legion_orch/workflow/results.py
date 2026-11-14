"""What actions return to their callers: an operator's human action (the UI and CLI) and an
agent's ``report`` (the MCP tool). Failures are raised, so a result always means success,
except a report, whose outcome is either accepted or the list of violated guards."""
from __future__ import annotations

from .model import NodeStatus, Record


class ActionResult(Record):
    """The result of a human action on a node or on the change."""
    status: NodeStatus | None = None        # the node's status afterwards (node actions)
    attestation: str | None = None          # the governance record written, if any
    warnings: list[str] = []
    note: str | None = None                 # something the operator should know
    # H-1 accept
    tier: str | None = None
    panel: list[str] | None = None
    # H-2 approve: subjects approved now, and what still blocks the gate
    approved: list[str] | None = None
    pending: list[str] | None = None
    # approve_surface
    surfaces: list[str] | None = None


class ReportOutcome(Record):
    """The result of an agent's report: accepted (with the node's next status) or rejected
    with the violated guards."""
    accepted: bool
    violations: list[str] = []
    attempts_left: int | None = None
    next: NodeStatus | None = None

    @classmethod
    def reject(cls, *violations: str, attempts_left: int | None = None) -> ReportOutcome:
        return cls(accepted=False, violations=list(violations), attempts_left=attempts_left)
