"""Running a workflow with dummy steps (plan: stages-and-steps.md §4): a runtime that carries out
the core's effects from scripts instead of sandboxes and agents, checking the invariants after
every event – so any structure and any sequence of actions can be tested in isolation.

A step's script is a list of outcomes, consumed one per start, resume or retry:
``ok`` and ``unchanged`` complete it, ``rework`` goes round its own loop once and continues,
``escalate:<kind>`` escalates it. A step with no script (or an exhausted one) completes ``ok``.
A gate's preconditions script is a list of unmet tuples, one per evaluation (default: all met).
"""
from __future__ import annotations

from collections import deque

from .core import (
    REMOVED,
    Completed,
    Effect,
    EndChange,
    Escalated,
    Evaluate,
    FlowState,
    Preconditions,
    ResumeStep,
    RetryStep,
    Reworking,
    StartStep,
    StopStep,
)
from .structure import Workflow


class InvariantError(AssertionError):
    pass


class ScriptedRuntime:
    def __init__(self, workflow: Workflow, steps: dict[str, list[str]] | None = None,
                 preconditions: dict[str, list[tuple[str, ...]]] | None = None, *,
                 hold: set[str] | frozenset[str] = frozenset()):
        """``hold``: steps that stay running when started (until :meth:`finish` or a script
        entry is fed), to test what happens while work is in flight."""
        self.flow = FlowState(workflow)
        self.scripts = {k: deque(v) for k, v in (steps or {}).items()}
        self.pre = {k: deque(v) for k, v in (preconditions or {}).items()}
        self.hold = set(hold)
        self.log: list[Effect] = []
        self.started: list[tuple[str, int]] = []
        self.ended: str | None = None
        self._attempts: dict[str, int] = {}
        self._checked = 0                                   # transitions checked so far
        self._last: dict[str, str] = {}                     # node -> status after its last one
        self._machines: dict[str, object] = {}

    # ------------------------------------------------------------------ driving
    def start(self) -> ScriptedRuntime:
        self._run(self.flow.start())
        return self

    def act(self, target: str, action: str, **kw) -> ScriptedRuntime:
        self._run(self.flow.act(target, action, **kw))
        return self

    def finish(self, step: str, outcome: str = "ok", attempt: int | None = None
               ) -> ScriptedRuntime:
        """A held step's outcome arrives (``attempt``: from that attempt – an earlier one is a late
        result the core ignores)."""
        self.hold.discard(step)
        self._run(self._outcome(step, outcome, attempt=attempt or self.attempt(step)))
        return self

    def send(self, event) -> ScriptedRuntime:
        self._run(self.flow.on(event))
        return self

    # ------------------------------------------------------------------ queries
    def status(self, nid: str) -> str:
        return self.flow.status(nid)

    def attempt(self, nid: str) -> int:
        return self.flow.attempt(nid)

    def waiting(self):
        return self.flow.waiting()

    def actions(self, nid: str) -> list[str]:
        return self.flow.actions(nid)

    def starts_of(self, step: str) -> list[StartStep]:
        return [e for e in self.log if isinstance(e, StartStep) and e.step == step]

    # ------------------------------------------------------------------ internals
    def _run(self, effects: list[Effect]) -> None:
        queue = deque(effects)
        while queue:
            e = queue.popleft()
            self._check_effect(e)
            self.log.append(e)
            events = []
            if isinstance(e, StartStep):
                self.started.append((e.step, e.attempt))
                if e.step not in self.hold:
                    events = self._next(e.step)
            elif isinstance(e, ResumeStep | RetryStep):
                if e.step not in self.hold:
                    events = self._next(e.step)
            elif isinstance(e, Evaluate):
                script = self.pre.get(e.gate)
                events = [Preconditions(e.gate, script.popleft() if script else ())]
            elif isinstance(e, EndChange):
                self.ended = e.status
            for ev in events:
                queue.extend(self.flow.on(ev))
                self._check_state()
        self._check_state()

    def _next(self, step: str) -> list:
        script = self.scripts.get(step)
        out = []
        while True:
            entry = script.popleft() if script else "ok"
            if entry != "rework":
                return out + self._outcome(step, entry, events_only=True)
            out.append(Reworking(step))

    def _outcome(self, step: str, entry: str, events_only: bool = False,
                 attempt: int | None = None):
        attempt = attempt or self.attempt(step)
        if entry in ("ok", "unchanged"):
            ev = Completed(step, entry, attempt)
        elif entry.startswith("escalate:"):
            kind = entry.split(":", 1)[1]
            ev = Escalated(step, kind, f"{step}: {kind}", attempt)
        else:
            raise ValueError(f"unknown script entry {entry!r}")
        return [ev] if events_only else self.flow.on(ev)

    def _check_effect(self, e: Effect) -> None:
        if self.ended and not isinstance(e, StopStep | EndChange):
            raise InvariantError(f"{e} after the change ended ({self.ended})")
        if isinstance(e, StartStep):
            last = self._attempts.get(e.step, 0)
            if e.attempt <= last:
                raise InvariantError(f"{e.step} started with attempt {e.attempt} after {last}")
            self._attempts[e.step] = e.attempt
        node = getattr(e, "step", None) or getattr(e, "gate", None) or getattr(e, "node", None)
        if node is not None and node not in self.flow.nodes and not isinstance(e, StopStep):
            raise InvariantError(f"{e} refers to an unknown node")

    def _check_state(self) -> None:
        if v := self.flow.violations():
            raise InvariantError("; ".join(v))
        self._check_transitions()

    def _check_transitions(self) -> None:
        """Every status change came through a declared transition, and each node's transitions
        chain: each starts where the previous one ended (from its initial status)."""
        for node, name, before, after in self.flow.log[self._checked:]:
            last = self._last.get(node)
            if name == REMOVED:                     # its machine is gone; a new one may follow
                if last is not None and before != last:
                    raise InvariantError(f"{node} removed at {before}, but it was {last}")
                self._last.pop(node, None)
                self._machines.pop(node, None)
                continue
            machine = type(self._machines.setdefault(node, self.flow.state.get(node)))
            sources, to = machine.transitions[name]
            if before not in sources or after != to:
                raise InvariantError(f"{node}.{name}: {before} -> {after} is not declared")
            last = last or machine.initial
            if before != last:
                raise InvariantError(f"{node}.{name} from {before}, but it was {last}")
            self._last[node] = after
        self._checked = len(self.flow.log)
