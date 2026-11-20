"""The workflow core (plan: stages-and-steps.md §1): pure and deterministic. It holds the stage
tree and every node's state, takes **events** (a step completed or escalated, preconditions
evaluated) and **actions** (generic: approve, send back, answer, retry, override, rerun,
evaluate, reject, abandon), and returns **effects** for a runtime to carry out (start a step, stop one,
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
* An escalated step may be answered by running an earlier, done, ``rerunnable`` step again in
  place (``rerun:<step>``: provisioning built again): that step starts a new attempt while what
  was built on it stands, and the escalated step is retried.
"""
from __future__ import annotations

from dataclasses import dataclass

from .states import (
    GateState,
    GateStatus,
    StageState,
    StageStatus,
    StepState,
    StepStatus,
)
from .structure import AppendRound, Gate, Parallel, Stage, Step, Workflow, walk

CHANGE = "change"                    # the action target for abandon
REMOVED = "removed"                  # a node dropped with its round (in the transition log)


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
    rerun: bool = False               # done before: it runs again in place


@dataclass(frozen=True)
class ResumeStep:
    step: str
    decision: str
    override: bool = False            # accepted without its own checks: it finishes its work


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


class FlowState:
    """One change's workflow in progress: the structure, and one state machine per node
    (:mod:`.states`). The methods here are the stage rules – when to start, complete or reset
    which node; every status change is a transition of the node's machine. Every method that
    changes state returns the effects."""

    def __init__(self, workflow: Workflow, attempts: dict[str, int] | None = None):
        """``attempts``: steps' attempts so far (a workflow rebuilt for a new structure
        continues them, so an attempt number is never reused)."""
        self.workflow = workflow
        self.nodes: dict[str, Stage | Step | Parallel | Gate] = {}
        self.parent: dict[str, Stage | Parallel | None] = {}
        self.stage_of_gate: dict[str, Stage] = {}
        self.state: dict[str, StepState | GateState | StageState] = {}
        self.rounds: dict[str, list[str]] = {}     # stage -> the ids of the rounds it inserted
        self.ended: str | None = None
        # (node, transition, from, to); a node removed with a dropped round logs ``removed``
        self.log: list[tuple[str, str, str, str]] = []
        # attempts of steps that were removed (a round dropped by a send-back): a step made again
        # with the same id continues from them, so an attempt number is never reused
        self._past_attempts: dict[str, int] = dict(attempts or {})
        self._effects: list[Effect] = []
        for s in workflow.stages:
            self._register(s, None)

    # ------------------------------------------------------------------ state access
    def step(self, nid: str) -> StepState:
        s = self.state[nid]
        assert isinstance(s, StepState), nid
        return s

    def gate(self, nid: str) -> GateState:
        s = self.state[nid]
        assert isinstance(s, GateState), nid
        return s

    def status(self, nid: str) -> str:
        return self.state[nid].status

    def attempt(self, nid: str) -> int:
        s = self.state.get(nid)
        return s.attempt if isinstance(s, StepState) else 0

    # ------------------------------------------------------------------ structure
    def _all(self):
        for s in self.workflow.stages:
            yield from walk(s)

    def _register(self, node, parent) -> None:
        if node.id in self.nodes:
            raise ValueError(f"duplicate node id {node.id!r}")
        self.nodes[node.id], self.parent[node.id] = node, parent
        if isinstance(node, Stage):
            if node.gate.id in self.nodes:
                raise ValueError(f"duplicate node id {node.gate.id!r}")
            self.nodes[node.gate.id], self.parent[node.gate.id] = node.gate, node
            self.stage_of_gate[node.gate.id] = node
            self._machine(node.id, StageState)
            self._machine(node.gate.id, GateState)
            for c in node.children:
                self._register(c, node)
        elif isinstance(node, Parallel):
            self._machine(node.id, StageState)
            ids = {s.id for s in node.steps}
            for st in node.steps:
                if unknown := set(st.depends_on) - ids:
                    raise ValueError(f"{st.id} depends on {sorted(unknown)} outside {node.id}")
                self._register(st, node)
        else:
            self._machine(node.id, StepState)

    def _machine(self, nid: str, cls) -> None:
        m = cls()
        m.watch(nid, self.log)
        if isinstance(m, StepState):
            m.attempt = self._past_attempts.get(nid, 0)
        self.state[nid] = m

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
            self.step(event.step).complete()
        elif isinstance(event, Escalated):
            self.step(event.step).escalate(event.kind, event.question)
        elif isinstance(event, Reworking):
            if self.status(event.step) == StepStatus.RUNNING:
                self.step(event.step).rework()
        elif isinstance(event, Preconditions):
            if self.status(event.gate) == GateStatus.WAITING:
                self.gate(event.gate).assessed(event.unmet)
        self._advance()
        return self._take()

    def _stale(self, event: Completed | Escalated) -> bool:
        st = self.state.get(event.step)
        return not isinstance(st, StepState) or st.status != StepStatus.RUNNING or (
            event.attempt is not None and event.attempt != st.attempt)

    def act(self, target: str, action: str, by: str = "human", text: str = "",
            subjects: list[str] | None = None) -> list[Effect]:
        """A human's action on ``target`` (a gate, a step or :data:`CHANGE`)."""
        if action not in self.actions(target):
            raise ActionError(f"{action!r} is not available on {target} "
                              f"(available: {self.actions(target)})")
        if action in ("answer", "override") and not text.strip():
            raise ActionError("an answer is required" if action == "answer"
                              else "a reason is required")
        if action == "abandon":
            self._end("abandoned")
        elif action.startswith("send_back:"):
            self._send_back(action.split(":", 1)[1], text, by)
        elif action == "approve":
            self._approve(target, subjects, by)
        elif action == "evaluate":
            self.gate(target).evaluating()
        elif action == "reject":
            self._effects.append(Record("reject", target, by, text))
            self._end("rejected")
        elif action == "answer":
            self.step(target).resume()
            self._effects += [Record("decision", target, by, text), ResumeStep(target, text)]
        elif action == "retry":
            self.step(target).resume()
            self._effects.append(RetryStep(target))
        elif action.startswith("rerun:"):
            again = action.split(":", 1)[1]
            st = self.step(again)
            if text:
                st.feedback.append(text)
            self._effects.append(Record("rerun", again, by, text))
            feedback = st.rerun()
            self._effects.append(StartStep(again, st.attempt, feedback, rerun=True))
            self.step(target).resume()
            self._effects.append(RetryStep(target))
        elif action == "override":
            # the step goes on to finish its work without its own checks (a build is merged);
            # it is done when that finishes
            self.step(target).resume()
            self._effects += [Record("override", target, by, text),
                              ResumeStep(target, text, override=True)]
        self._advance()
        return self._take()

    def define(self, group: str, steps: list[Step]) -> list[Effect]:
        """The steps of a :class:`Parallel` group, once they are known (a task graph accepted):
        they replace the group's earlier ones. Only before the group has started – after that
        its work is redone by sending a stage back, which resets it first. A step defined again
        with an earlier id continues from its attempts."""
        node = self.nodes.get(group)
        if not isinstance(node, Parallel):
            raise ActionError(f"{group!r} is not a parallel group")
        if self.status(group) != StageStatus.PENDING:
            raise ActionError(f"{group} has already started")
        for st in node.steps:
            self._forget(st.id)
        node.steps = list(steps)
        ids = {s.id for s in steps}
        for st in steps:
            if unknown := set(st.depends_on) - ids:
                raise ValueError(f"{st.id} depends on {sorted(unknown)} outside {group}")
            self._register(st, node)
        self._advance()
        return self._take()

    def redo(self, step: str, feedback: str = "", by: str = "controller") -> list[Effect]:
        """A step done or under way must do its work again (a later step in its stage found it
        wanting: preflight's failed setup sends the oracle back). The step and everything after
        it in its stage – its gate included – go back to pending, and the step starts again
        with the feedback. Only while its stage runs, and only for a step directly in a stage."""
        node = self.nodes.get(step)
        parent = self.parent.get(step)
        if not isinstance(node, Step) or not isinstance(parent, Stage):
            raise ActionError(f"{step!r} is not a step directly in a stage")
        if self.status(parent.id) != StageStatus.RUNNING:
            raise ActionError(f"{parent.id} is not running")
        order = [n.id for n in walk(parent)]
        for nid in order[order.index(step):]:
            self._reset_one(nid)
        if feedback:
            self.step(step).feedback.append(feedback)
        self._effects.append(Record("redo", step, by, feedback))
        self._advance()
        return self._take()

    def actions(self, target: str) -> list[str]:
        """What a human may do on ``target`` now – derived from the structure."""
        if self.ended:
            return []
        if target == CHANGE:
            return ["abandon"]
        node, st = self.nodes.get(target), self.state.get(target)
        out: list[str] = []
        if isinstance(node, Gate) and st.status == GateStatus.WAITING:
            if not node.automatic and self._missing(node):
                out.append("approve")
            if node.preconditions:
                out.append("evaluate")
            if node.rejectable:
                out.append("reject")
        elif isinstance(node, Step) and st.status == StepStatus.ESCALATED:
            out += ["answer", "retry"] + (["override"] if node.overridable else [])
            out += [f"rerun:{nid}" for nid in self.rerunnable(target)]
        else:
            return []
        out += [f"send_back:{s}" for s in self.built_on(target)]
        return out + ["abandon"]

    def rerunnable(self, node_id: str) -> list[str]:
        """The done steps before ``node_id`` (in execution order) that may run again in place."""
        order = self._order()
        before = order[:order.index(node_id)] if node_id in order else []
        return [nid for nid in before if isinstance(self.nodes[nid], Step)
                and self.nodes[nid].rerunnable and self.status(nid) == StepStatus.DONE]

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
            st = self.state[nid]
            if isinstance(st, StepState) and st.status == StepStatus.ESCALATED:
                kind, q = st.escalation
                out.append(Waiting(nid, kind, q))
            elif isinstance(st, GateState) and st.status == GateStatus.WAITING:
                if st.unmet:
                    out.append(Waiting(nid, "preconditions", ", ".join(st.unmet)))
                elif not self.nodes[nid].automatic and self._missing(self.nodes[nid]):
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
        st = self.state[node.id]
        if st.status in (StepStatus.DONE, StageStatus.DONE):
            return True
        if isinstance(node, Step):
            if st.status == StepStatus.PENDING:
                self._start(node.id)
            return False
        if st.status == StageStatus.PENDING:
            st.start()
        if isinstance(node, Parallel):
            for s in node.steps:
                if self.status(s.id) == StepStatus.PENDING and \
                        all(self.status(d) == StepStatus.DONE for d in s.depends_on):
                    self._start(s.id)
            if all(self.status(s.id) == StepStatus.DONE for s in node.steps):
                st.complete()
                return True
            return False
        for child in node.children:                          # a stage
            if not self._run(child):
                return False
        if self._gate(node.gate):
            st.complete()
            return True
        return False

    def _start(self, step: str) -> None:
        st = self.step(step)
        feedback = st.start()
        self._effects.append(StartStep(step, st.attempt, feedback))

    def _subjects(self, gate: Gate) -> list[str]:
        return gate.subjects or [gate.id]

    def _missing(self, gate: Gate) -> set[str]:
        return set(self._subjects(gate)) - set(self.gate(gate.id).approvals)

    def _gate(self, gate: Gate) -> bool:
        """The gate's turn: True when it is done (approved and preconditions met)."""
        g = self.gate(gate.id)
        if g.status == GateStatus.PENDING:
            g.open()
        if gate.automatic and not g.approvals:
            for s in self._subjects(gate):
                g.approve(s, "controller")
        if gate.preconditions and g.unmet is None:
            self._effects.append(Evaluate(gate.id))
            g.assessed(("(being evaluated)",))
            return False
        if not self._missing(gate) and not (gate.preconditions and g.unmet):
            g.complete()
            if gate.automatic:
                self._effects.append(Record("approval", gate.id, "controller", "automatic"))
            return True
        return False

    def _approve(self, gate_id: str, subjects: list[str] | None, by: str) -> None:
        gate, g = self.nodes[gate_id], self.gate(gate_id)
        wanted = subjects or sorted(self._missing(gate), key=self._subjects(gate).index)
        if unknown := set(wanted) - set(self._subjects(gate)):
            raise ActionError(f"unknown subject(s) {sorted(unknown)} of {gate_id}")
        for s in wanted:
            g.approve(s, by)
            self._effects.append(Record("approval", gate_id, by, s))
        if gate.preconditions:
            g.evaluating()                  # evaluated again at the approval

    # ------------------------------------------------------------------ rework
    def _send_back(self, stage_id: str, feedback: str, by: str) -> None:
        stage = self.nodes[stage_id]
        assert isinstance(stage, Stage)
        self._effects.append(Record("send_back", stage_id, by, feedback))
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
            st = self.state[a.id]
            if st.status != StageStatus.PENDING:
                st.reopen()
            self.gate(a.gate.id).reset()
        if target and feedback:
            self.step(target).feedback.append(feedback)

    def _after(self, stage_id: str) -> list[str]:
        """Every node after ``stage_id``'s subtree in execution order."""
        order = self._order()
        inside = {n.id for n in walk(self.nodes[stage_id])}
        last = max(order.index(n) for n in inside)
        return order[last + 1:]

    def _append_round(self, stage: Stage) -> str | None:
        policy = stage.rework
        assert isinstance(policy, AppendRound)
        rounds = self.rounds.setdefault(stage.id, [])
        rnd = policy.template(len(rounds) + 2)            # round 1 is the original work
        at = (len(stage.children) if policy.before is None
              else next(i for i, c in enumerate(stage.children) if c.id == policy.before))
        stage.children.insert(at, rnd)
        self._register(rnd, stage)
        rounds.append(rnd.id)
        for child in stage.children[at + 1:]:
            self._reset(child)
        self.gate(stage.gate.id).reset()
        st = self.state[stage.id]
        if st.status != StageStatus.PENDING:
            st.reopen()
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
        st = self.state.get(nid)
        if st is None:
            return
        if isinstance(st, StepState) and st.status == StepStatus.RUNNING:
            self._effects.append(StopStep(nid))
        st.reset()

    def _forget(self, nid: str) -> None:
        st = self.state.get(nid)
        if isinstance(st, StepState):
            self._past_attempts[nid] = st.attempt
            if st.status == StepStatus.RUNNING:
                self._effects.append(StopStep(nid))
        if st is not None:
            self.log.append((nid, REMOVED, st.status, REMOVED))
        for d in (self.nodes, self.parent, self.state, self.stage_of_gate):
            d.pop(nid, None)

    def _end(self, status: str) -> None:
        for nid, st in list(self.state.items()):
            if isinstance(st, StepState) and st.status in (StepStatus.RUNNING,
                                                            StepStatus.ESCALATED):
                if st.status == StepStatus.RUNNING:
                    self._effects.append(StopStep(nid))
                st.reset()                    # never to run: the change has ended
        self.ended = status
        self._effects.append(EndChange(status))

    # ------------------------------------------------------------------ invariants
    def violations(self) -> list[str]:
        """The state invariants (plan §1); empty when they hold."""
        v = []
        done = (StepStatus.DONE, StageStatus.DONE, GateStatus.DONE)

        def finished(k: str) -> bool:      # done, or done before and running again in place
            node = self.nodes[k]
            return self.status(k) in done or (isinstance(node, Step) and node.rerunnable
                                              and self.step(k).in_place)
        for nid in self._order():
            node, st = self.nodes[nid], self.state[nid]
            if isinstance(node, Stage):
                kids = [c.id for c in node.children]
                gate = self.status(node.gate.id)
                if st.status == StageStatus.DONE and (
                        gate != GateStatus.DONE or any(not finished(k) for k in kids)):
                    v.append(f"{nid} done before its children and gate")
                if gate in (GateStatus.WAITING, GateStatus.DONE) and \
                        any(not finished(k) for k in kids):
                    v.append(f"{node.gate.id} reached before {nid}'s children were done")
                active = [k for k in kids if not finished(k) and self.status(k) != "pending"]
                if len(active) > 1:
                    v.append(f"{nid} runs several children at once: {active}")
                for i, k in enumerate(kids):
                    if not finished(k) and \
                            any(self.status(x) != "pending" for x in kids[i + 1:]):
                        v.append(f"{nid}: a child after unfinished {k} has started")
                        break
            if isinstance(node, Gate):
                if st.status == GateStatus.DONE and self._missing(node):
                    v.append(f"{nid} done without every subject approved")
                if st.status == GateStatus.WAITING:
                    for later in self._after(self.stage_of_gate[nid].id):
                        if self.status(later) in (StepStatus.RUNNING, StepStatus.ESCALATED) \
                                and isinstance(self.nodes[later], Step):
                            v.append(f"{later} runs while {nid} waits")
            if isinstance(st, StepState) and st.status == StepStatus.ESCALATED and \
                    st.escalation is None:
                v.append(f"{nid} escalated without a reason")
        if self.ended and any(isinstance(st, StepState) and st.status != StepStatus.PENDING
                              and st.status != StepStatus.DONE for st in self.state.values()):
            v.append("a step runs or waits after the change ended")
        return v
