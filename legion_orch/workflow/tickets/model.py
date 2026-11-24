"""The ticket: the one unit of the workflow (plan: ~/plans/tickets-design.md).

A ticket is its own work (by its type's executor; none for a ``grouping`` ticket), then its
subtasks – groups run in order, a group's tickets in parallel – then its criteria, in order. A
criterion is a ticket too: a leaf whose outcome is a verdict. Plain records: what happens to them
is :mod:`.rules`'s business."""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

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
    BLOCKED = "blocked"        # its agent is blocked on a human (a BLOCKED report: never an attempt)
    EMPTY = "empty"            # a grouping ticket with no subtasks


class Remedy(StrEnum):
    RETRY = "retry"            # the same ticket, another round (same id, same attempt count)
    WAIVE = "waive"            # a failed, waivable criterion counts as passed
    REPLAN = "replan"          # the constraints must change: a plan fails it or adds to it


# the remedies each reason for waiting allows (a replan is always possible)
REMEDIES: dict[Waiting, set[Remedy]] = {
    Waiting.ATTEMPTS: {Remedy.WAIVE, Remedy.REPLAN},
    Waiting.REPEATED: {Remedy.WAIVE, Remedy.REPLAN},
    Waiting.ESCALATED: {Remedy.WAIVE, Remedy.REPLAN},
    Waiting.SYSTEM: {Remedy.RETRY, Remedy.REPLAN},
    Waiting.BUDGET: {Remedy.RETRY, Remedy.REPLAN},
    Waiting.BLOCKED: {Remedy.RETRY, Remedy.REPLAN},
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
    inputs: list[str] = []                 # the pointers it consumes (approved outputs, by name)
    work: dict[str, Any] = {}              # its role's contract (the role's work schema): an
                                           # implementer's instructions, files, verification …
    subtasks: list[list[str]] = []         # groups of child ids: in order, a group's in parallel
    criteria: list[str] = []               # child ids, run in order after the work and subtasks
    severity: Severity = Severity.REWORK   # (criteria) what a failure does to the parent
    waivable: bool = True                  # (criteria) whether a human may waive a failure
    signs: list[str] = []                  # (sign-offs) the outputs it signs (by name; ``head``)
    applies: str | None = None             # (sign-offs) what its pass applies: the output it signs
                                           # that is a replan (``tickets``), or ``tier``
    limit: int = 3                         # attempts allowed (its own work's reruns)
    keep: bool = False                     # (an agent's) its sandbox stays, stopped, after it closes
                                           # until its parent does: others consult its agent
    # state
    status: Status = Status.PENDING
    attempt: int = 0                       # rounds of its own work so far (0: not started)
    sha: str | None = None                 # its commit: the accepted report's; a branch owner's
                                           # branch head once a child merged (merged on pass)
    findings: list[str] = []               # (a ticket) the failed criterion's findings, for its rework
    waiting: Waiting | None = None
    waived: bool = False                   # (criteria) passed by a human's waiver
    outputs: dict[str, str | list[str]] = {}  # what its accepted report attests: name -> doc ref(s)
    runs: list[str] = []                   # its agent runs, in order (the current: the last)
    sandbox: str | None = None             # the sandbox its own work keeps across its attempts
    since: str = ""                        # when its status last changed (the engine stamps it)
    pointers: dict[str, str | list[str]] = {}  # (the root) the approved version of each output
    held: Literal["start", "retry"] | None = None   # (paused) what it does on resume
    paused: list[str] = []                 # (the root) why the workflow is paused: none – it runs
    merging: bool = False                  # its merge is owed (merging, or waiting after a merge error)

    @property
    def has_work(self) -> bool:
        return self.type != GROUPING

    @property
    def owns_branch(self) -> bool:
        """Whether it has a branch of its own (decided by the tree, never by a plan): the root
        (the change's result), an agent's ticket (its work may commit), a grouping ticket with
        criteria (they judge its head: its subtree's work alone). Anything else works on, and
        merges into, its nearest ancestor's that has one."""
        return self.parent is None or (self.role != "criterion" and self.type.startswith("agent:")) \
            or (self.type == GROUPING and bool(self.criteria))

    @property
    def leaf(self) -> bool:
        return not self.subtasks and not self.criteria
