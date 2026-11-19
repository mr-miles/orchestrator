"""The workflow core (plan: stages-and-steps.md §1): pure and deterministic. It holds the stage
tree and every node's state, takes **events** (a step completed or escalated, preconditions
evaluated) and **actions** (generic: approve, send back, answer, retry, override, evaluate,
reject, abandon), and returns **effects** for a runtime to carry out (start a step, stop one,
evaluate a gate, record a decision, end the change). It does no I/O.

Rules:

* A stage runs its children in order; a :class:`Parallel` group starts its steps as their
  dependencies allow. When every child is done the stage's gate waits – for its subjects'
  approvals (or approves itself, if automatic) and for its preconditions – and completes when both
  hold.
* Sending a stage back applies its rework policy: :class:`StartOver` resets everything in it,
  :class:`AppendRound` keeps the work done and inserts a round. Either way everything after the
  stage, in execution order, is reset too (it was built on it), and the stage's ancestors are
  running again (their gates reset).
* The actions a node offers are derived from the structure (:meth:`FlowState.actions`): any stage
  the node's work was built on – the stages enclosing it and those before them – may be sent back,
  so a "replan" is just a send-back of the stage that holds the plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .structure import AppendRound, Gate, Parallel, Stage, Step, Workflow, walk

PENDING, RUNNING, ESCALATED, WAITING, DONE = "pending", "running", "escalated", "waiting", "done"
CHANGE = "change"                    # the action target for abandon


# ---------------------------------------------------------------------------- events
@dataclass(frozen=True)
class Completed:
    step: str
    outcome: str = "ok"               # ok | unchanged
    attempt: int | None = None        # the attempt it is about (None: the current one)


@dataclass(frozen=True)
class Escalated:
    step: str
    kind: str
    question: str = ""
    attempt: int | None = None


@dataclass(frozen=True)
class Reworking:
    """The step's own loop went round (informational: the core counts it)."""
    step: str


@dataclass(frozen=True)
class Preconditions:
    gate: str
    unmet: tuple[str, ...] = ()


Event = Completed | Escalated | Reworking | Preconditions


# ---------------------------------------------------------------------------- effects
@dataclass(frozen=True)
class StartStep:
    step: str
    attempt: int
    feedback: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResumeStep:
    step: str
    decision: str


@dataclass(frozen=True)
class RetryStep:
    step: str


@dataclass(frozen=True)
class StopStep:
    step: str


@dataclass(frozen=True)
class Evaluate:
    gate: str


@dataclass(frozen=True)
class Record:
    kind: str                         # approval | override | send_back | decision
    node: str
    by: str
    detail: str = ""


@dataclass(frozen=True)
class EndChange:
    status: str                       # completed | abandoned | rejected


Effect = StartStep | ResumeStep | RetryStep | StopStep | Evaluate | Record | EndChange


class ActionError(ValueError):
    """An action the node does not offer now."""


@dataclass(frozen=True)
class Waiting:
    node: str
    reason: str                       # approval | preconditions | <escalation kind>
    detail: str = ""


@dataclass
class _Gate:
    approvals: dict[str, str] = field(default_factory=dict)      # subject -> approver
    unmet: tuple[str, ...] | None = None                         # None: not evaluated


class FlowState:
    """One change's workflow in progress. Every method that changes state returns the effects."""

    def __init__(self, workflow: Workflow):
        self.workflow = workflow
        self.status: dict[str, str] = {}
        self.attempt: dict[str, int] = {}
        self.cycles: dict[str, int] = {}           # the steps' own rework loops
        self.feedback: dict[str, list[str]] = {}
        self.escalation: dict[str, tuple[str, str]] = {}
        self.gates: dict[str, _Gate] = {}
        self.rounds: dict[str, list[str]] = {}     # stage -> the ids of the rounds it inserted
        self.ended: str | None = None
        self._effects: list[Effect] = []
        self._index()
        for n in self._all():
            self.status.setdefault(n.id, PENDING)

    # ------------------------------------------------------------------ structure
    def _all(self):
        for s in self.workflow.stages:
            yield from walk(s)

    def _index(self) -> None:
        self.nodes: dict[str, object] = {}
        self.parent: dict[str, Stage | Parallel | None] = {}
        self.stage_of_gate: dict[str, Stage] = {}
        for s in self.workflow.stages:
            self._register(s, None)

    def _register(self, node, parent) -> None:
        if node.id in self.nodes:
            raise ValueError(f"duplicate node id {node.id!r}")
        self.nodes[node.id], self.parent[node.id] = node, parent
        if isinstance(node, Stage):
            if node.gate.id in self.nodes:
                raise ValueError(f"duplicate node id {node.gate.id!r}")
            self.nodes[node.gate.id], self.parent[node.gate.id] = node.gate, node
            self.stage_of_gate[node.gate.id] = node
            self.gates.setdefault(node.gate.id, _Gate())
            for c in node.children:
                self._register(c, node)
        elif isinstance(node, Parallel):
            ids = {s.id for s in node.steps}
            for st in node.steps:
                if unknown := set(st.depends_on) - ids:
                    raise ValueError(f"{st.id} depends on {sorted(unknown)} outside {node.id}")
                self._register(st, node)

    def ancestors(self, node_id: str) -> list[Stage]:
        """The stages ``node_id`` is in, nearest first (a gate is in its own stage)."""
        out, p = [], self.parent.get(node_id)
        if isinstance(self.nodes.get(node_id), Gate):
            out.append(self.stage_of_gate[node_id])
            p = self.parent.get(self.stage_of_gate[node_id].id)
        while p is not None:
            if isinstance(p, Stage):
                out.append(p)
            p = self.parent.get(p.id)
        return out

    def _order(self) -> list[str]:
        return [n.id for n in self._all()]

    def _first_step(self, stage: Stage) -> str | None:
        if stage.feedback_to:
            return stage.feedback_to
        return next((n.id for n in walk(stage) if isinstance(n, Step)), None)

    # ------------------------------------------------------------------ public
    def start(self) -> list[Effect]:
        self._advance()
        return self._take()

    def on(self, event: Event) -> list[Effect]:
        if self.ended:
            return []
        if isinstance(event, Completed | Escalated) and self._stale(event):
            return []                        # stopped, reset or restarted meanwhile
        if isinstance(event, Completed):
            self.status[event.step] = DONE
        elif isinstance(event, Escalated):
            self.status[event.step] = ESCALATED
            self.escalation[event.step] = (event.kind, event.question)
        elif isinstance(event, Reworking):
            self.cycles[event.step] = self.cycles.get(event.step, 0) + 1
        elif isinstance(event, Preconditions):
            if self.status.get(event.gate) == WAITING:
                self.gates[event.gate].unmet = tuple(event.unmet)
        self._advance()
        return self._take()

    def _stale(self, event: Completed | Escalated) -> bool:
        return self.status.get(event.step) != RUNNING or (
            event.attempt is not None and event.attempt != self.attempt.get(event.step))

    def act(self, target: str, action: str, by: str = "human", text: str = "",
            subjects: list[str] | None = None) -> list[Effect]:
        """A human's action on ``target`` (a gate, a step or :data:`CHANGE`)."""
        if action not in self.actions(target):
            raise ActionError(f"{action!r} is not available on {target} "
                              f"(available: {self.actions(target)})")
        if action == "abandon":
            self._end("abandoned")
        elif action.startswith("send_back:"):
            self._send_back(action.split(":", 1)[1], text, by)
        elif action == "approve":
            self._approve(target, subjects, by)
        elif action == "evaluate":
            self.gates[target].unmet = None
        elif action == "reject":
            self._effects.append(Record("reject", target, by, text))
            self._end("rejected")
        elif action == "answer":
            if not text.strip():
                raise ActionError("an answer is required")
            self.status[target] = RUNNING
            self.escalation.pop(target, None)
            self._effects += [Record("decision", target, by, text), ResumeStep(target, text)]
        elif action == "retry":
            self.status[target] = RUNNING
            self.escalation.pop(target, None)
            self._effects.append(RetryStep(target))
        elif action == "override":
            if not text.strip():
                raise ActionError("a reason is required")
            self.status[target] = DONE
            self.escalation.pop(target, None)
            self._effects.append(Record("override", target, by, text))
        self._advance()
        return self._take()

    def actions(self, target: str) -> list[str]:
        """What a human may do on ``target`` now – derived from the structure."""
        if self.ended:
            return []
        if target == CHANGE:
            return ["abandon"]
        node, st = self.nodes.get(target), self.status.get(target)
        out: list[str] = []
        if isinstance(node, Gate) and st == WAITING:
            g = self.gates[target]
            if not node.automatic and set(self._subjects(node)) - set(g.approvals):
                out.append("approve")
            if node.preconditions:
                out.append("evaluate")
            if node.rejectable:
                out.append("reject")
        elif isinstance(node, Step) and st == ESCALATED:
            out += ["answer", "retry"] + (["override"] if node.overridable else [])
        else:
            return []
        out += [f"send_back:{s}" for s in self.built_on(target)]
        return out + ["abandon"]

    def built_on(self, node_id: str) -> list[str]:
        """The stages ``node_id``'s work was built on, nearest first: at each level up, the stage
        that encloses it, then the stages before that one. Any of them may be sent back."""
        out: list[str] = []
        for stage in self.ancestors(node_id):
            out.append(stage.id)
            parent = self.parent.get(stage.id)
            siblings = parent.children if isinstance(parent, Stage) else self.workflow.stages
            before = siblings[:[c.id for c in siblings].index(stage.id)]
            out += [c.id for c in reversed(before) if isinstance(c, Stage)]
        return list(dict.fromkeys(out))

    def waiting(self) -> list[Waiting]:
        """What waits for a human, in execution order (nothing, once the change has ended)."""
        if self.ended:
            return []
        out = []
        for nid in self._order():
            st = self.status[nid]
            if st == ESCALATED:
                kind, q = self.escalation[nid]
                out.append(Waiting(nid, kind, q))
            elif st == WAITING:
                node, g = self.nodes[nid], self.gates[nid]
                if g.unmet:
                    out.append(Waiting(nid, "preconditions", ", ".join(g.unmet)))
                elif not node.automatic and set(self._subjects(node)) - set(g.approvals):
                    out.append(Waiting(nid, "approval"))
        return out

    # ------------------------------------------------------------------ running
    def _take(self) -> list[Effect]:
        out, self._effects = self._effects, []
        return out

    def _advance(self) -> None:
        if self.ended:
            return
        for stage in self.workflow.stages:
            if not self._run(stage):
                return
        self._end("completed")

    def _run(self, node) -> bool:
        """Start what can start in ``node``; True when it is done."""
        st = self.status[node.id]
        if st == DONE:
            return True
        if isinstance(node, Step):
            if st == PENDING:
                self._start(node.id)
            return False
        if isinstance(node, Parallel):
            self.status[node.id] = RUNNING
            for s in node.steps:
                if self.status[s.id] == PENDING and all(self.status[d] == DONE
                                                        for d in s.depends_on):
                    self._start(s.id)
            if all(self.status[s.id] == DONE for s in node.steps):
                self.status[node.id] = DONE
                return True
            return False
        self.status[node.id] = RUNNING                     # a stage
        for child in node.children:
            if not self._run(child):
                return False
        if self._gate(node.gate):
            self.status[node.id] = DONE
            return True
        return False

    def _start(self, step: str) -> None:
        self.attempt[step] = self.attempt.get(step, 0) + 1
        self.status[step] = RUNNING
        self._effects.append(StartStep(step, self.attempt[step],
                                       tuple(self.feedback.pop(step, []))))

    def _subjects(self, gate: Gate) -> list[str]:
        return gate.subjects or [gate.id]

    def _gate(self, gate: Gate) -> bool:
        """The gate's turn: True when it is done (approved and preconditions met)."""
        g = self.gates[gate.id]
        if self.status[gate.id] == PENDING:
            self.status[gate.id] = WAITING
        if gate.automatic and not g.approvals:
            g.approvals = {s: "controller" for s in self._subjects(gate)}
        if gate.preconditions and g.unmet is None:
            self._effects.append(Evaluate(gate.id))
            g.unmet = ("(being evaluated)",)
            return False
        approved = not set(self._subjects(gate)) - set(g.approvals)
        if approved and not (gate.preconditions and g.unmet):
            self.status[gate.id] = DONE
            if gate.automatic:
                self._effects.append(Record("approval", gate.id, "controller", "automatic"))
            return True
        return False

    def _approve(self, gate_id: str, subjects: list[str] | None, by: str) -> None:
        gate, g = self.nodes[gate_id], self.gates[gate_id]
        wanted = subjects or [s for s in self._subjects(gate) if s not in g.approvals]
        if unknown := set(wanted) - set(self._subjects(gate)):
            raise ActionError(f"unknown subject(s) {sorted(unknown)} of {gate_id}")
        for s in wanted:
            g.approvals[s] = by
            self._effects.append(Record("approval", gate_id, by, s))
        if gate.preconditions:
            g.unmet = None                  # evaluated again at the approval

    # ------------------------------------------------------------------ rework
    def _send_back(self, stage_id: str, feedback: str, by: str) -> None:
        stage = self.nodes[stage_id]
        assert isinstance(stage, Stage)
        self._effects.append(Record("send_back", stage_id, by, feedback))
        if self.status[stage_id] == DONE:
            self.status[stage_id] = RUNNING
        later = self._after(stage_id)
        if isinstance(stage.rework, AppendRound):
            target = self._append_round(stage)
        else:
            self._reset(stage)
            target = self._first_step(stage)
        for nid in later:                 # a whole stage resets as one (it loses its rounds)
            node = self.nodes.get(nid)
            if isinstance(node, Stage):
                self._reset(node)
            else:
                self._reset_one(nid)
        for a in self.ancestors(stage_id):
            self.status[a.id] = RUNNING
            self._reset_gate(a.gate)
        if target and feedback:
            self.feedback.setdefault(target, []).append(feedback)

    def _after(self, stage_id: str) -> list[str]:
        """Every node after ``stage_id``'s subtree in execution order, except its ancestors' gates
        (reset separately)."""
        order = self._order()
        inside = {n.id for n in walk(self.nodes[stage_id])}
        last = max(order.index(n) for n in inside)
        return order[last + 1:]

    def _append_round(self, stage: Stage) -> str | None:
        policy = stage.rework
        assert isinstance(policy, AppendRound)
        rounds = self.rounds.setdefault(stage.id, [])
        rnd = policy.template(len(rounds) + 2)            # round 1 is the original work
        at = next(i for i, c in enumerate(stage.children) if c.id == policy.before)
        stage.children.insert(at, rnd)
        self._register(rnd, stage)
        for n in walk(rnd):
            self.status.setdefault(n.id, PENDING)
        rounds.append(rnd.id)
        for child in stage.children[at + 1:]:
            self._reset(child)
        self._reset_gate(stage.gate)
        self.status[stage.id] = RUNNING
        return self._first_step(rnd)

    def _reset(self, node) -> None:
        """Back to pending: ``node`` and everything in it (a round-appending stage loses the
        rounds it inserted: they were built on what is being redone)."""
        if isinstance(node, Stage) and self.rounds.get(node.id):
            gone = set(self.rounds.pop(node.id))
            for rnd in [c for c in node.children if c.id in gone]:
                for n in walk(rnd):
                    self._forget(n.id)
            node.children = [c for c in node.children if c.id not in gone]
        for n in walk(node):
            self._reset_one(n.id)

    def _reset_one(self, nid: str) -> None:
        node = self.nodes.get(nid)
        if node is None:
            return
        if isinstance(node, Gate):
            self._reset_gate(node)
            return
        if self.status.get(nid) == RUNNING and isinstance(node, Step):
            self._effects.append(StopStep(nid))
        self.status[nid] = PENDING
        self.escalation.pop(nid, None)

    def _reset_gate(self, gate: Gate) -> None:
        self.status[gate.id] = PENDING
        self.gates[gate.id] = _Gate()

    def _forget(self, nid: str) -> None:
        if self.status.get(nid) == RUNNING and isinstance(self.nodes.get(nid), Step):
            self._effects.append(StopStep(nid))
        for d in (self.nodes, self.parent, self.status, self.escalation, self.gates,
                  self.stage_of_gate):
            d.pop(nid, None)

    def _end(self, status: str) -> None:
        for nid, st in list(self.status.items()):
            if st in (RUNNING, ESCALATED) and isinstance(self.nodes.get(nid), Step):
                if st == RUNNING:
                    self._effects.append(StopStep(nid))
                self.status[nid] = PENDING           # never to run: the change has ended
                self.escalation.pop(nid, None)
        self.ended = status
        self._effects.append(EndChange(status))

    # ------------------------------------------------------------------ invariants
    def violations(self) -> list[str]:
        """The state invariants (plan §1); empty when they hold."""
        v = []
        for nid in self._order():
            node, st = self.nodes[nid], self.status[nid]
            if isinstance(node, Stage):
                kids = [c.id for c in node.children]
                if st == DONE and (self.status[node.gate.id] != DONE
                                   or any(self.status[k] != DONE for k in kids)):
                    v.append(f"{nid} done before its children and gate")
                if self.status[node.gate.id] in (WAITING, DONE) and \
                        any(self.status[k] != DONE for k in kids):
                    v.append(f"{node.gate.id} reached before {nid}'s children were done")
                active = [k for k in kids if self.status[k] not in (PENDING, DONE)]
                if len(active) > 1:
                    v.append(f"{nid} runs several children at once: {active}")
                for i, k in enumerate(kids):
                    if self.status[k] != DONE and any(self.status[x] != PENDING
                                                      for x in kids[i + 1:]):
                        v.append(f"{nid}: a child after unfinished {k} has started")
                        break
            if isinstance(node, Gate):
                g = self.gates[nid]
                if st == DONE and set(self._subjects(node)) - set(g.approvals):
                    v.append(f"{nid} done without every subject approved")
                if st == WAITING:
                    for later in self._after(self.stage_of_gate[nid].id):
                        if self.status.get(later) in (RUNNING, ESCALATED):
                            v.append(f"{later} runs while {nid} waits")
            if isinstance(node, Step) and st == ESCALATED and nid not in self.escalation:
                v.append(f"{nid} escalated without a reason")
        if self.ended and any(st == RUNNING and isinstance(self.nodes[n], Step)
                              for n, st in self.status.items()):
            v.append("a step runs after the change ended")
        return v
