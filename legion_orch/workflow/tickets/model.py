"""The ticket: the one unit of the workflow (plan: ~/plans/tickets-design.md).

A ticket is its own work (by its type's executor; none for a ``grouping`` ticket), then its
subtasks – groups run in order, a group's tickets in parallel – then its criteria, in order. A
criterion is a ticket too: a leaf whose outcome is a verdict. Plain records: what happens to them
is :mod:`.rules`'s business."""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

GROUPING = "grouping"


class Status(StrEnum):
    PENDING = "pending"        # not started
    WORKING = "working"        # its own work is under way (an agent's run, a controller action)
    SUBTASKS = "subtasks"      # its subtask groups are running
    CHECKING = "checking"      # its criteria are running, in order
    MERGING = "merging"        # passed its criteria; its commit is being merged into the parent's
    PAUSED = "paused"          # at a conclusion point while the workflow is paused (``held``: next)
    WAITING = "waiting"        # on a human: an escalation (``waiting`` says why)
    PASSED = "passed"          # done: closed – nothing is ever added under it
    REJECTED = "rejected"      # a criterion whose verdict was fail
    FAILED = "failed"          # a replan replaced it: immutable history
    CANCELLED = "cancelled"    # an accepted replan no longer needs it


ACTIVE = {Status.WORKING, Status.MERGING}       # what a replan waits for
SETTLED = {Status.PASSED, Status.CANCELLED}     # a subtask group is complete when all are


class Severity(StrEnum):
    """What a criterion's failure does to the ticket it checks."""
    REWORK = "rework"          # the ticket's own work runs again with the findings (an attempt)
    ESCALATE = "escalate"      # the ticket escalates at once (always, on a grouping ticket)


class Waiting(StrEnum):
    """Why a ticket waits on a human – and so which remedies apply."""
    ATTEMPTS = "attempts"      # its attempts are used up (agents looping)
    REPEATED = "repeated"      # a criterion failed with the same findings as on the last attempt
    ESCALATED = "escalated"    # an ``escalate`` criterion failed
    SYSTEM = "system"          # a system error outlasted its backoff (never an attempt)
    BUDGET = "budget"          # its budget is exceeded (never an attempt)
    EMPTY = "empty"            # a grouping ticket with no subtasks


class Remedy(StrEnum):
    RETRY = "retry"            # the same ticket, another round (same id, same attempt count)
    WAIVE = "waive"            # a failed, waivable criterion counts as passed
    REPLAN = "replan"          # the constraints must change: the ticket becomes failed


# the remedies each reason for waiting allows (a replan is always possible)
REMEDIES: dict[Waiting, set[Remedy]] = {
    Waiting.ATTEMPTS: {Remedy.WAIVE, Remedy.REPLAN},
    Waiting.REPEATED: {Remedy.WAIVE, Remedy.REPLAN},
    Waiting.ESCALATED: {Remedy.WAIVE, Remedy.REPLAN},
    Waiting.SYSTEM: {Remedy.RETRY, Remedy.REPLAN},
    Waiting.BUDGET: {Remedy.RETRY, Remedy.REPLAN},
    Waiting.EMPTY: {Remedy.REPLAN},
}


class Ticket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    parent: str | None = None              # the root (the change) has none
    type: str = GROUPING                   # grouping | agent:<role> | human:signoff | controller:<action>
    goal: str = ""
    role: Literal["root", "subtask", "criterion"] = "subtask"
    labels: list[str] = []                 # risk labels; effective = own ∪ the parent's (add-only)
    inputs: list[str] = []                 # what it is given (attestations)
    subtasks: list[list[str]] = []         # groups of child ids: in order, a group's in parallel
    criteria: list[str] = []               # child ids, run in order after the work and subtasks
    severity: Severity = Severity.REWORK   # (criteria) what a failure does to the parent
    waivable: bool = True                  # (criteria) whether a human may waive a failure
    signs: list[str] = []                  # (human:signoff) what it signs: documents at versions
    applies: str | None = None             # (sign-offs) what its pass applies: a replan document,
                                           # or ``tier`` (the tier's skeleton tickets)
    branch: str | None = None              # a named branch instead of its own (``integration``)
    limit: int = 3                         # attempts allowed (its own work's reruns)
    # state
    status: Status = Status.PENDING
    attempt: int = 0                       # rounds of its own work so far (0: not started)
    sha: str | None = None                 # its commit: the accepted report's; a grouping ticket's
                                           # branch head once a child merged (merged on pass)
    findings: list[str] = []               # (a ticket) the failed criterion's findings, for its rework
    waiting: Waiting | None = None
    waived: bool = False                   # (criteria) passed by a human's waiver
    held: Literal["start", "retry"] | None = None   # (paused) what it does on resume
    paused: list[str] = []                 # (the root) why the workflow is paused: none – it runs
    merging: bool = False                  # its merge is owed (merging, or waiting after a merge error)

    @property
    def has_work(self) -> bool:
        return self.type != GROUPING

    @property
    def leaf(self) -> bool:
        return not self.subtasks and not self.criteria
