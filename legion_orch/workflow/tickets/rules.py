"""The ticket rules: pure decisions over a change's tickets (plan: ~/plans/tickets-design.md).

``Tickets`` holds the tickets and applies events – work done, a verdict, an error, a human's remedy,
an accepted replan – returning the effects for the runner to carry out (start or stop work, merge,
escalate, end). No I/O: the same inputs give the same tickets and effects, so property tests can
drive any tree through any sequence.

The rules (the numbered ones are ~/plans/tickets.md's corner-case rules):

* a ticket is its own work (none for grouping), then its subtask groups – in order, a group's
  tickets in parallel – then its criteria, in order, stopping at the first failure;
* a grouping ticket with no subtasks waits at once (``empty``): no stage is legitimately empty;
* a criterion's failure: ``rework`` – the ticket's own work again with the findings (an attempt),
  every criterion rerun from the first –; ``escalate`` – the ticket waits; on a grouping ticket a
  failure always escalates (R4); the same findings as last time escalate early (R16);
* reaching the attempt limit makes a ticket wait – it becomes failed (immutable, removed from its
  parent's lists) only when a human chooses a replan;
* a restart (a build, never a grouping ticket or one with subtasks) clears the ticket – attempts,
  findings, commit, sandbox, runs, criteria – and starts it afresh;
* system errors and an exceeded budget make a ticket wait; neither is an attempt (R14);
* a ticket with a commit that passes its criteria is merged into its parent's branch first
  (``merging``): merged, it passes and the parent's commit is the new head; a conflict is a failed
  "can merge" – rework (R8); a passed ticket is closed (R5);
* the workflow can be paused (``pause``, with a reason; it runs again once no reason is left):
  then nothing starts – no ticket, no criterion, grouping tickets included; work under way goes
  on to its next conclusion point (its work done, its verdict given, its merge made – a merge
  that is due concludes work, so it is never held) and rests there (``paused``, ``held``: what it
  does next); on resume every ticket goes on from where it rests;
* a replan applies only while the workflow is paused and nothing is active (a static backdrop):
  choosing a replan pauses it, applying one lifts that pause; a sign-off that applies a replan
  pauses the workflow while it is under way, and applies it with its verdict. A replan adds
  tickets only after a ticket's completed groups (and never under a closed ticket), cancels
  and updates tickets not yet started, and fails work that extra tickets cannot resolve.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .model import (
    ACTIVE,
    GROUPING,
    SETTLED,
    Remedy,
    Severity,
    Status,
    Ticket,
    Waiting,
    allowed_remedies,
)


# ------------------------------------------------------------------------------------- effects
@dataclass(frozen=True)
class StartWork:
    ticket: str
    attempt: int
    findings: tuple[str, ...] = ()         # what the last failed criterion found (a rework)


@dataclass(frozen=True)
class StopWork:
    ticket: str


@dataclass(frozen=True)
class Merge:
    ticket: str
    sha: str
    into: str                              # the parent: its branch


@dataclass(frozen=True)
class Escalate:
    ticket: str
    reason: Waiting


@dataclass(frozen=True)
class Ended:
    status: Status                         # passed | cancelled


Effect = StartWork | StopWork | Merge | Escalate | Ended


class TicketError(ValueError):
    """An event the rules refuse (the ticket is not where the event says it is)."""


@dataclass
class Add:
    """A replan's new ticket: under ``into``, as a subtask in group ``group`` (a new group at that
    index when ``new_group``) or – ``criterion`` – as a criterion at index ``group``. ``tree``:
    the ticket's descendants, which come with it (already listed by their parents)."""
    ticket: Ticket
    into: str
    group: int
    new_group: bool = True
    criterion: bool = False
    tree: list[Ticket] = field(default_factory=list)


@dataclass
class Replan:
    cancel: list[str] = field(default_factory=list)
    fail: list[str] = field(default_factory=list)
    add: list[Add] = field(default_factory=list)
    update: dict[str, dict[str, Any]] = field(default_factory=dict)


UPDATABLE = {"goal", "inputs", "labels", "limit", "severity", "waivable", "signs", "type", "work"}


REPLANNING = "replan"                         # the pause a chosen replan makes


def signing(tid: str) -> str:
    """The pause a sign-off that applies a replan makes while it is under way."""
    return f"signing {tid}"


# ------------------------------------------------------------------------------------- rules
class Tickets:
    def __init__(self, tickets: Iterable[Ticket]):
        self.t: dict[str, Ticket] = {}
        for x in tickets:
            if x.id in self.t:
                raise TicketError(f"duplicate ticket id {x.id!r}")
            self.t[x.id] = x
        if errs := validate(self.t):
            raise TicketError("; ".join(errs))
        self._effects: list[Effect] = []

    @property
    def root(self) -> Ticket:
        return next(x for x in self.t.values() if x.parent is None)

    def labels(self, tid: str) -> set[str]:
        """A ticket's effective risk labels: its own and its ancestors' (inherited, add-only)."""
        out, x = set(), self.t.get(tid)
        while x is not None:
            out |= set(x.labels)
            x = self.t.get(x.parent) if x.parent else None
        return out

    # ------------------------------------------------------------------------- events
    def start(self) -> list[Effect]:
        root = self.root
        if root.status != Status.PENDING:
            raise TicketError("the change has started")
        self._start(root)
        return self._take()

    def work_done(self, tid: str, sha: str | None = None,
                  outputs: dict[str, str | list[str]] | None = None) -> list[Effect]:
        """The ticket's own work is done: its accepted report (``sha``: the commit it attests;
        ``outputs``: the documents it names, pinned)."""
        x = self._working(tid, criterion=False)
        x.sha = sha
        x.outputs = dict(outputs or {})
        self._after_work(x)
        return self._take()

    def verdict(self, tid: str, passed: bool, findings: Iterable[str] = (),
                apply: Replan | None = None,
                signed: dict[str, str | list[str]] | None = None) -> list[Effect]:
        """A criterion's outcome. ``apply``: the replan its pass applies (a sign-off of the
        planner's tickets, of a replan document, of the tier) – in the same step, so nothing
        starts before it is applied; every other ticket must be inactive. ``signed``: what a
        sign-off approved (name -> the exact versions) – its pass moves the change's pointers
        to them, in the same step."""
        c = self._working(tid, criterion=True)
        if apply is not None and passed:
            self._inactive(but=tid)
        with self._transaction():
            c.findings = list(findings)
            c.status = Status.PASSED if passed else Status.REJECTED
            parent = self.t[c.parent]
            if passed and signed:
                self.root.pointers.update(signed)
            if not passed:
                self._criterion_failed(parent, c)
            elif apply is not None and not self._edit(apply):
                return self._take()                       # it cancelled the root
            elif parent.status == Status.CHECKING:
                self._check(parent)
        if signing(c.id) in self.root.paused:             # its pause is over, either way
            return self.resume(signing(c.id))
        return self._take()

    def work_failed(self, tid: str, findings: Iterable[str] = ()) -> list[Effect]:
        """The agent's turn ended without an acceptable outcome (it gave up, its report
        submissions ran out): an attempt – again with ``findings``, or wait at the limit."""
        x = self._working(tid)
        self._again(x, list(findings))
        return self._take()

    def merged(self, tid: str, head: str) -> list[Effect]:
        """The ticket's commit is merged into its parent's branch, whose head is now ``head``."""
        x = self._merging(tid)
        x.merging = False
        self.t[self.merges_into(tid)].sha = head
        self._passed(x)
        return self._take()

    def merge_conflict(self, tid: str, findings: Iterable[str] = ()) -> list[Effect]:
        """The commit no longer merges cleanly (a sibling merged first): a failed "can merge" –
        rework onto the new head (R8); a grouping ticket, with no work of its own, escalates."""
        x = self._merging(tid)
        x.merging = False
        if x.has_work:
            self._again(x, list(findings))
        else:
            x.findings = list(findings)
            self._wait(x, Waiting.ESCALATED)
        return self._take()

    def system_error(self, tid: str, findings: Iterable[str] = ()) -> list[Effect]:
        """A system error (the work's or the merge's) outlasted its backoff: wait for a human
        (never an attempt); ``findings``: what failed, for them."""
        x = self.t.get(tid)
        x = x if x is not None and x.status == Status.MERGING else self._working(tid)
        if findings:
            x.findings = list(findings)
        self._wait(x, Waiting.SYSTEM)
        return self._take()

    def blocked(self, tid: str, findings: Iterable[str] = ()) -> list[Effect]:
        """Its agent is blocked on a human (a decision it lacks): wait – not an attempt; a retry
        runs the same attempt again, told what it was missing (``findings``)."""
        x = self._working(tid)
        x.findings = list(findings)
        self._wait(x, Waiting.BLOCKED)
        return self._take()

    def budget_exceeded(self, tid: str, findings: Iterable[str] = ()) -> list[Effect]:
        x = self._working(tid)
        if findings:
            x.findings = list(findings)
        self._wait(x, Waiting.BUDGET)
        return self._take()

    @classmethod
    def over(cls, t: dict[str, Ticket]) -> Tickets:
        """The rules over a dict of tickets kept elsewhere (the engine's state store): the same
        dict, never a copy. Checked when it holds a tree (an empty one is a change not started)."""
        x = cls.__new__(cls)
        x.t, x._effects = t, []
        if t and (errs := validate(t)):
            raise TicketError("; ".join(errs))
        return x

    @property
    def paused(self) -> bool:
        return bool(self.root.paused)

    def pause(self, reason: str = "operator") -> list[Effect]:
        """Nothing starts from now on (``reason``: who paused it; each lifts only its own)."""
        if reason not in self.root.paused:
            self.root.paused.append(reason)
        return self._take()

    def resume(self, reason: str = "operator") -> list[Effect]:
        """``reason``'s pause lifted: with none left, every ticket goes on from where it rests."""
        if reason in self.root.paused:
            self.root.paused.remove(reason)
        self._wake()
        return self._take()

    def merges_into(self, tid: str) -> str:
        """The ticket whose branch ``tid``'s commit merges into (and its branch starts from):
        its nearest ancestor that owns a branch."""
        p = self.t[self.t[tid].parent]                 # type: ignore[index]
        while not p.owns_branch:
            p = self.t[p.parent]                       # type: ignore[index]
        return p.id

    def active(self) -> list[str]:
        return [x.id for x in self.t.values() if x.status in ACTIVE]

    def decide(self, tid: str, remedy: Remedy) -> list[Effect]:
        """A human's remedy on a waiting ticket – or, ``waive``, on a rejected criterion of one."""
        x = self.t[tid]
        if remedy == Remedy.WAIVE and x.role == "criterion":
            parent = self.t[x.parent]
            if x.status != Status.REJECTED or parent.status != Status.WAITING:
                raise TicketError(f"{tid} is not a rejected criterion of a waiting ticket")
            if not x.waivable:
                raise TicketError(f"{tid} cannot be waived")
            x.status, x.waived = Status.PASSED, True
            parent.status, parent.waiting = Status.CHECKING, None
            self._check(parent)
            return self._take()
        if x.status != Status.WAITING or remedy not in allowed_remedies(x):
            raise TicketError(f"{remedy} is not a remedy for {tid} ({x.status}, {x.waiting})")
        if remedy == Remedy.RETRY:                  # the same ticket, another round (or merge)
            x.waiting = None
            self._go(x, "merge" if x.merging else "retry")
        elif remedy == Remedy.RESTART:              # afresh: as if never started (limit kept)
            if REPLANNING in self.root.paused:
                raise TicketError(f"a replan is chosen: {tid} cannot restart")
            x.attempt, x.findings, x.waiting, x.sha, x.sandbox = 0, [], None, None, None
            x.outputs, x.runs, x.held, x.merging = {}, [], None, False
            self._rerun_criteria(x, everyone=True)
            self._go(x, "start")
        elif remedy == Remedy.REPLAN:               # the constraints must change: a static
            self.pause(REPLANNING)                  # backdrop until the plan is applied (it
                                                    # fails the ticket, or adds to it)
        return self._take()

    def replan(self, plan: Replan, *, trial: bool = False) -> list[Effect]:
        """An accepted replan (signed off), applied while the workflow is paused and nothing is
        active – all of it or, refused, none of it; the replan's pause is lifted. ``trial``: on
        a copy, to see the tree it makes (whatever is active)."""
        if not trial:
            self._inactive()
        with self._transaction():
            self._edit(plan)
        return self.resume(REPLANNING)

    def _inactive(self, but: str | None = None) -> None:
        if not self.paused:
            raise TicketError("a replan is applied while the workflow is paused")
        if busy := [t for t in self.active() if t != but]:
            raise TicketError(f"a replan waits for every ticket to be inactive: {busy} active")

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        """Everything in the block, or – a refusal – nothing."""
        saved = {k: v.model_copy(deep=True) for k, v in self.t.items()}
        effects = list(self._effects)
        try:
            yield
        except TicketError:
            self.t.clear()                         # in place: the dict may be shared (the store's)
            self.t.update(saved)
            self._effects = effects
            raise

    def _edit(self, plan: Replan) -> bool:
        """The replan's edits, then the tickets it touched go on; False: it cancelled the root."""
        touched: set[str] = set()
        for tid in plan.fail:
            x = self.t[tid]
            if x.status in (Status.PASSED, Status.FAILED, Status.CANCELLED):
                raise TicketError(f"{tid} is {x.status}: it cannot be failed")
            touched.add(x.parent)
            self._fail(x)
        for tid in plan.cancel:
            x = self.t[tid]
            if x.status != Status.PENDING:
                raise TicketError(f"{tid} has started: only tickets not yet started are cancelled")
            if x.parent is None:
                self._cancel_tree(x)
                self._effects.append(Ended(Status.CANCELLED))
                return False
            touched.add(x.parent)
            self._remove(x)
            self._cancel_tree(x)
        for tid, fields in plan.update.items():
            x = self.t[tid]
            if x.status != Status.PENDING:
                raise TicketError(f"{tid} has started: only tickets not yet started are updated")
            if bad := set(fields) - UPDATABLE:
                raise TicketError(f"{tid}: {sorted(bad)} cannot be updated")
            for k, v in fields.items():
                setattr(x, k, v)
        for a in plan.add:
            self._add(a)
            touched.add(a.into)
        if errs := validate(self.t):
            raise TicketError("; ".join(errs))
        for tid in sorted(touched, key=self._depth, reverse=True):     # deepest first
            self._resume(self.t[tid])
        return True

    def abandon(self) -> list[Effect]:
        """The change ends where it is (R17): running work stops, everything not finished is
        cancelled (passed and failed tickets stay as they are)."""
        for x in self.t.values():
            if x.status == Status.WORKING:
                self._effects.append(StopWork(x.id))
            x.merging = False
            if x.status not in (Status.PASSED, Status.FAILED, Status.CANCELLED):
                x.status, x.waiting = Status.CANCELLED, None
        self._effects.append(Ended(Status.CANCELLED))
        return self._take()

    # ------------------------------------------------------------------------- the machine
    def _take(self) -> list[Effect]:
        out, self._effects = self._effects, []
        return out

    def _merging(self, tid: str) -> Ticket:
        x = self.t.get(tid)
        if x is None or x.status != Status.MERGING:
            raise TicketError(f"{tid} is not merging")
        return x

    def _working(self, tid: str, criterion: bool | None = None) -> Ticket:
        x = self.t.get(tid)
        if x is None or x.status != Status.WORKING:
            raise TicketError(f"{tid} is not working")
        if criterion is not None and (x.role == "criterion") != criterion:
            raise TicketError(f"{tid} is {'not ' if criterion else ''}a criterion")
        return x

    def _go(self, x: Ticket, held: str) -> None:
        """``x`` goes on – or, paused, rests until the resume (``held``: how it goes on). A
        merge goes on even then: it concludes work already done (paused, it could be lost)."""
        if held == "merge":
            x.status, x.merging = Status.MERGING, True
            self._effects.append(Merge(x.id, x.sha, self.merges_into(x.id)))  # type: ignore[arg-type]
        elif self.paused:
            x.status, x.held = Status.PAUSED, held      # type: ignore[assignment]
        elif held == "retry":
            x.status = Status.WORKING
            self._effects.append(StartWork(x.id, x.attempt, tuple(x.findings)))
        else:
            x.status = Status.PENDING
            self._start(x)

    def _wake(self) -> None:
        """No pause left: every ticket goes on from where it rests – the paused ones as they
        were held, then (deepest first) the ones whose next subtask or criterion is due."""
        if self.paused:
            return
        root = self.root
        if root.status == Status.PENDING and root.attempt == 0 and not root.has_work:
            self._start(root)
        for x in [x for x in self.t.values() if x.status == Status.PAUSED]:
            held, x.held = x.held, None
            self._go(x, held)                                       # type: ignore[arg-type]
        for x in sorted(self.t.values(), key=lambda x: self._depth(x.id), reverse=True):
            if x.status == Status.SUBTASKS:
                self._subtasks(x)
            elif x.status == Status.CHECKING:
                self._check(x)

    def _start(self, x: Ticket) -> None:
        if self.paused:                             # nothing starts: it rests where it is
            if x.status != Status.PENDING:
                x.status, x.held = Status.PAUSED, "start"
            return
        if x.has_work:
            if x.role == "criterion" and x.applies:  # a static backdrop while it is signed
                self.pause(signing(x.id))
            x.attempt += 1
            x.status = Status.WORKING
            self._effects.append(StartWork(x.id, x.attempt, tuple(x.findings)))
        elif not any(x.subtasks):
            self._wait(x, Waiting.EMPTY)
        else:
            x.status = Status.SUBTASKS
            self._subtasks(x)

    def _after_work(self, x: Ticket) -> None:
        if any(x.subtasks):
            x.status = Status.SUBTASKS
            self._subtasks(x)
        else:
            self._check(x)

    def _subtasks(self, x: Ticket) -> None:
        """Start the first unsettled group's tickets not yet started; all settled: check."""
        for group in x.subtasks:
            members = [self.t[m] for m in group]
            if all(m.status in SETTLED for m in members):
                continue
            for m in members:
                if m.status == Status.PENDING:
                    self._start(m)
            return
        self._check(x)

    def _check(self, x: Ticket) -> None:
        """The first criterion not yet passed this attempt starts; all passed: pass."""
        x.status = Status.CHECKING
        for cid in x.criteria:
            c = self.t[cid]
            if c.status == Status.PASSED:
                continue
            if c.status == Status.PENDING:
                self._start(c)
            return
        self._pass(x)

    def _criterion_failed(self, x: Ticket, c: Ticket) -> None:
        if c.severity == Severity.ESCALATE or not x.has_work:      # R4: a grouping ticket escalates
            self._wait(x, Waiting.ESCALATED)
        elif c.findings and c.findings == x.findings:              # R16: the same again
            self._wait(x, Waiting.REPEATED)
        else:
            self._again(x, c.findings)

    def _again(self, x: Ticket, findings: list[str]) -> None:
        """Another round of the ticket's own work (an attempt), its criteria all rerun – or, at
        the limit, wait for a human."""
        if x.attempt >= x.limit:
            self._wait(x, Waiting.ATTEMPTS)
            return
        x.findings = list(findings)
        self._rerun_criteria(x)
        self._start(x)

    def _wait(self, x: Ticket, why: Waiting) -> None:
        x.status, x.waiting = Status.WAITING, why
        self._effects.append(Escalate(x.id, why))

    def _pass(self, x: Ticket) -> None:
        """Its criteria passed: a subtask with a commit merges first; anything else passes."""
        if x.sha and x.role == "subtask":
            x.merging = True
            self._go(x, "merge")
        else:
            self._passed(x)

    def _passed(self, x: Ticket) -> None:
        x.status = Status.PASSED
        if x.parent is None:
            self._effects.append(Ended(Status.PASSED))
            return
        parent = self.t[x.parent]
        if parent.status == Status.SUBTASKS:
            self._subtasks(parent)

    def _fail(self, x: Ticket) -> None:
        """Failed: immutable history, out of its parent's lists (new tickets take its place)."""
        x.status, x.waiting = Status.FAILED, None
        self._remove(x)

    def _remove(self, x: Ticket) -> None:
        if x.parent is None:
            return
        p = self.t[x.parent]
        p.subtasks = [g for g in ([m for m in g if m != x.id] for g in p.subtasks) if g]
        p.criteria = [c for c in p.criteria if c != x.id]

    def _cancel_tree(self, x: Ticket) -> None:
        x.status = Status.CANCELLED
        for cid in [m for g in x.subtasks for m in g] + x.criteria:
            if self.t[cid].status not in (Status.FAILED,):
                self._cancel_tree(self.t[cid])

    def _add(self, a: Add) -> None:
        x, p = a.ticket, self.t.get(a.into)
        if p is None:
            raise TicketError(f"no ticket {a.into!r} to add {x.id} under")
        if x.id in self.t:
            raise TicketError(f"duplicate ticket id {x.id!r}")
        if p.status in (Status.PASSED, Status.FAILED, Status.CANCELLED):
            raise TicketError(f"{p.id} is {p.status}: nothing is added under it")
        x = x.model_copy(update={"parent": p.id, "role": "criterion" if a.criterion else "subtask",
                                 "status": Status.PENDING, "attempt": 0})
        if a.criterion:
            done = len([c for c in p.criteria if self.t[c].status == Status.PASSED])
            if a.group < done:
                raise TicketError(f"{x.id}: a criterion is added only after the passed ones")
            p.criteria.insert(a.group, x.id)
        else:
            done = 0
            for g in p.subtasks:
                if not all(self.t[m].status in SETTLED for m in g):
                    break
                done += 1
            if a.group < done:
                raise TicketError(f"{x.id}: tickets are added only after {p.id}'s completed groups")
            if a.new_group or a.group >= len(p.subtasks):
                p.subtasks.insert(a.group, [x.id])
            elif any(self.t[m].status != Status.PENDING for m in p.subtasks[a.group]):
                raise TicketError(f"{x.id}: group {a.group} of {p.id} has started")
            else:
                p.subtasks[a.group].append(x.id)
        self.t[x.id] = x
        below = {x.id}
        for d in a.tree:                          # parents before their children
            if d.id in self.t:
                raise TicketError(f"duplicate ticket id {d.id!r}")
            if d.parent not in below:
                raise TicketError(f"{d.id}: its parent {d.parent!r} is not in {x.id}'s tree")
            self.t[d.id] = d.model_copy(update={"status": Status.PENDING, "attempt": 0})
            below.add(d.id)

    def _resume(self, x: Ticket) -> None:
        """After a replan: a ticket it touched goes on from where it is. One waiting on its
        criteria (escalated) or its subtasks (empty) is resumed. New subtasks run first, and
        then every criterion runs again, from the first – they judge a new head (fresh
        judges); without new subtasks only the criteria not passed run again."""
        if x.status == Status.WAITING and x.waiting not in (Waiting.EMPTY, Waiting.ESCALATED):
            return
        if x.status not in (Status.SUBTASKS, Status.CHECKING, Status.WAITING):
            return
        unsettled = any(not all(self.t[m].status in SETTLED for m in g) for g in x.subtasks)
        if x.status in (Status.CHECKING, Status.WAITING):
            x.waiting = None
            self._rerun_criteria(x, everyone=unsettled)
        if unsettled:
            x.status = Status.SUBTASKS
            self._subtasks(x)
        else:
            self._check(x)

    def _rerun_criteria(self, x: Ticket, everyone: bool = True) -> None:
        """Its criteria again, as new (fresh judges): all of them, or those not passed."""
        for cid in x.criteria:
            c = self.t[cid]
            if everyone or c.status != Status.PASSED:
                c.status, c.findings, c.waived, c.attempt = Status.PENDING, [], False, 0

    def _depth(self, tid: str) -> int:
        n, x = 0, self.t[tid]
        while x.parent:
            n, x = n + 1, self.t[x.parent]
        return n


# ------------------------------------------------------------------------------------- shape
def _path(t: dict[str, Ticket], tid: str) -> list[tuple[str, int]]:
    """Where a ticket sits, from the root down: each ancestor and the position in it (its subtask
    group's index; a criterion's, after every group)."""
    out, x = [], t[tid]
    while x.parent is not None:
        p = t[x.parent]
        if x.id in p.criteria:
            pos = len(p.subtasks) + p.criteria.index(x.id)
        else:
            pos = next((i for i, g in enumerate(p.subtasks) if x.id in g), -1)
        out.append((p.id, pos))
        x = p
    return out[::-1]


def precedes(t: dict[str, Ticket], a: str, b: str) -> bool:
    """``a`` is finished before ``b`` starts: at their nearest common ancestor, ``a`` sits in an
    earlier subtask group (or ``a`` is a subtask, ``b`` a criterion; or both are criteria, ``a``
    first). Tickets in one group run in parallel; an ancestor is never before its descendants."""
    pa, pb = _path(t, a) + [(a, -2)], _path(t, b) + [(b, -2)]
    for (ancestor_a, pos_a), (ancestor_b, pos_b) in zip(pa, pb, strict=False):
        if ancestor_a != ancestor_b:
            return False
        if pos_a != pos_b:
            return 0 <= pos_a < pos_b
    return False                                       # the same ticket, or one inside the other


def validate(t: dict[str, Ticket]) -> list[str]:
    """The tree's structural rules (the tier policy's are checked elsewhere)."""
    v: list[str] = []
    roots = [x.id for x in t.values() if x.parent is None]
    if len(roots) != 1:
        v.append(f"exactly one root ticket, not {roots}")
    for x in t.values():
        if x.parent is None:
            if x.role != "root":
                v.append(f"{x.id}: the root's role is root")
            continue
        p = t.get(x.parent)
        if p is None:
            v.append(f"{x.id}: unknown parent {x.parent}")
            continue
        if x.status in (Status.FAILED, Status.CANCELLED):
            continue                                # history: out of the lists
        listed_sub = any(x.id in g for g in p.subtasks)
        listed_crit = x.id in p.criteria
        if x.role == "subtask" and not listed_sub or x.role == "criterion" and not listed_crit:
            v.append(f"{x.id}: not in {p.id}'s {x.role}s")
        if x.role == "criterion":
            if not x.leaf:
                v.append(f"{x.id}: a criterion is a leaf (no subtasks or criteria)")
            if not x.has_work:
                v.append(f"{x.id}: a criterion is not a grouping ticket")
            if p.type == GROUPING and x.severity != Severity.ESCALATE:
                v.append(f"{x.id}: a grouping ticket's criteria escalate (R4)")
    for x in t.values():
        for cid in [m for g in x.subtasks for m in g] + x.criteria:
            if cid not in t or t[cid].parent != x.id:
                v.append(f"{x.id}: lists {cid}, which is not its child")
        seen, y = set(), x
        while y.parent:                              # no cycles
            if y.id in seen:
                v.append(f"{x.id}: its parents form a cycle")
                break
            seen.add(y.id)
            y = t.get(y.parent) or y.model_copy(update={"parent": None})
    return v
