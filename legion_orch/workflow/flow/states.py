"""The three node state machines (plan: stages-and-steps.md §1): a step, a gate, and a stage (or
a parallel group). Each status is an enumeration; each change of status is a named transition
that declares the statuses it may start from. The decorator enforces that – an impossible
transition raises instead of leaving a wrong status – and records it in the class's
``transitions`` table, which tests and diagrams read (it is never written by hand).

The machines know nothing about each other: when to start, complete or reset what is the stage
rules' business (:mod:`.core`)."""
from __future__ import annotations

import functools
from enum import StrEnum
from typing import ClassVar


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    ESCALATED = "escalated"
    DONE = "done"


class GateStatus(StrEnum):
    PENDING = "pending"
    WAITING = "waiting"
    DONE = "done"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"


class IllegalTransition(RuntimeError):
    """A transition called from a status it does not start from: a bug in the stage rules."""


def transition(*sources, to):
    """Mark a method as the transition ``sources`` → ``to``."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            if self._status not in sources:
                raise IllegalTransition(
                    f"{type(self).__name__}.{fn.__name__}: not from {self._status} "
                    f"(only from {', '.join(sorted(sources))})")
            before = self._status
            out = fn(self, *args, **kwargs)
            self._status = to
            if self._log is not None:
                self._log.append((self._id, fn.__name__, before, to))
            return out
        wrapper.transition = (frozenset(sources), to)
        return wrapper
    return deco


class Machine:
    """A node's state: ``status`` changes only through its transitions."""
    transitions: ClassVar[dict[str, tuple[frozenset, StrEnum]]] = {}
    initial: ClassVar[StrEnum]

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls.transitions = {name: fn.transition for name, fn in vars(cls).items()
                           if callable(fn) and hasattr(fn, "transition")}

    def __init__(self):
        self._status = self.initial
        self._id: str | None = None
        self._log: list | None = None

    @property
    def status(self):
        return self._status

    def watch(self, node_id: str, log: list) -> None:
        """Record every transition in ``log`` as (node, transition, from, to)."""
        self._id, self._log = node_id, log


class StepState(Machine):
    """pending → running ⇄ escalated → done (→ running again, run in place); any → pending (sent
    back, or the change ended)."""
    initial = StepStatus.PENDING
    S = StepStatus

    def __init__(self):
        super().__init__()
        self.attempt = 0                      # how many times the core started it
        self.cycles = 0                       # its own rework loops (informational)
        self.feedback: list[str] = []         # for its next start (a send-back's)
        self.escalation: tuple[str, str] | None = None     # (kind, question)
        self.in_place = False                 # running again after it was done (rerun)

    @transition(S.PENDING, to=S.RUNNING)
    def start(self) -> tuple[str, ...]:
        """A new attempt; returns the feedback it starts with."""
        self.in_place = False
        self.attempt += 1
        fb, self.feedback = tuple(self.feedback), []
        return fb

    @transition(S.DONE, to=S.RUNNING)
    def rerun(self) -> tuple[str, ...]:
        """Done, it runs again in place (a new attempt); what was built on it stands."""
        self.in_place = True
        self.attempt += 1
        fb, self.feedback = tuple(self.feedback), []
        return fb

    @transition(S.RUNNING, to=S.RUNNING)
    def rework(self) -> None:
        self.cycles += 1

    @transition(S.RUNNING, to=S.DONE)
    def complete(self) -> None:
        pass

    @transition(S.RUNNING, to=S.ESCALATED)
    def escalate(self, kind: str, question: str) -> None:
        self.escalation = (kind, question)

    @transition(S.ESCALATED, to=S.RUNNING)
    def resume(self) -> None:
        """Answered, or retried: it continues in the same attempt."""
        self.escalation = None

    @transition(S.ESCALATED, to=S.DONE)
    def override(self) -> None:
        self.escalation = None

    @transition(*S, to=S.PENDING)
    def reset(self) -> None:
        self.escalation, self.in_place = None, False


class GateState(Machine):
    """pending → waiting → done; while waiting it collects approvals and preconditions results;
    any → pending (its stage starts again)."""
    initial = GateStatus.PENDING
    S = GateStatus

    def __init__(self):
        super().__init__()
        self.approvals: dict[str, str] = {}               # subject -> approver
        self.unmet: tuple[str, ...] | None = None          # None: not evaluated (yet, or again)

    @transition(S.PENDING, to=S.WAITING)
    def open(self) -> None:
        pass

    @transition(S.WAITING, to=S.WAITING)
    def approve(self, subject: str, by: str) -> None:
        self.approvals[subject] = by

    @transition(S.WAITING, to=S.WAITING)
    def evaluating(self) -> None:
        """Its preconditions are being evaluated (again)."""
        self.unmet = None

    @transition(S.WAITING, to=S.WAITING)
    def assessed(self, unmet: tuple[str, ...]) -> None:
        self.unmet = tuple(unmet)

    @transition(S.WAITING, to=S.DONE)
    def complete(self) -> None:
        pass

    @transition(*S, to=S.PENDING)
    def reset(self) -> None:
        self.approvals, self.unmet = {}, None


class StageState(Machine):
    """pending → running → done (a stage, or a parallel group); running again when something in
    it is sent back; any → pending (reset with what it was built on)."""
    initial = StageStatus.PENDING
    S = StageStatus

    @transition(S.PENDING, to=S.RUNNING)
    def start(self) -> None:
        pass

    @transition(S.RUNNING, to=S.DONE)
    def complete(self) -> None:
        pass

    @transition(S.RUNNING, S.DONE, to=S.RUNNING)
    def reopen(self) -> None:
        pass

    @transition(*S, to=S.PENDING)
    def reset(self) -> None:
        pass
