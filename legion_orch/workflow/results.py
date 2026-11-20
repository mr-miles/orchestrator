"""What actions return to their callers: an operator's human action (the UI and CLI) and an
agent's ``report`` (the MCP tool). Failures are raised, so a result always means success,
except a report, whose outcome is either accepted or the list of violated guards."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Feedback, NodeStatus, Record, Resume


# ---------------------------------------------------------------------------- phase results
# How every phase of a step ends (engine/phases.py), the agent's included: its role handler
# (workflow/roles/) returns one for an accepted report, and the phase applies it.
@dataclass(frozen=True)
class Ok:
    """The phase passed; ``value`` is what it produced (a merge's new head, a readiness
    result, built images)."""
    value: Any = None


@dataclass(frozen=True)
class Rework:
    """The producer has to do its work again, with this feedback (within its cycle budget).
    ``base``: the commit its next cycle starts from, if that moved (a merge conflict).
    ``budget_checked``: the producer's own budget was checked already (the critique's, G-C2)."""
    feedback: list[Feedback]
    source: str
    base: str | None = None
    budget_checked: bool = False


@dataclass(frozen=True)
class Escalate:
    """A human decides; ``resume`` says where the node continues once they have. ``feedback``
    is kept on the node for the producer's next cycle; ``run``: the run it is about."""
    kind: str
    question: str
    resume: Resume
    log_tail: str | None = None
    feedback: list[Feedback] = field(default_factory=list)
    run: str | None = None
    missing_decision: str | None = None


PhaseResult = Ok | Rework | Escalate


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
    with the violated guards. A dry run is never accepted: ``would_accept`` says whether the
    same report would be, and ``violations`` lists what it would be rejected for."""
    accepted: bool
    violations: list[str] = []
    attempts_left: int | None = None
    next: NodeStatus | None = None
    dry_run: bool | None = None
    would_accept: bool | None = None

    @classmethod
    def reject(cls, *violations: str, attempts_left: int | None = None) -> ReportOutcome:
        return cls(accepted=False, violations=list(violations), attempts_left=attempts_left)
