"""The workflow runtime (plan: stages-and-steps.md §1, principle 2): the change's stage rules run
in the pure core (:mod:`..flow`); this carries out its effects on the engine and tells it what
happened.

Who decides what:

* **between steps – the core:** what starts when, gates, send-backs, fix rounds, the end;
* **within a step – the engine:** its phases, rework cycles, escalations and their answers
  (:class:`.StepRunner`, the role handlers, :mod:`.human`).

:meth:`FlowRuntime.sync` runs on every save: an accepted node completes its step, an escalated
one escalates it, a resolved escalation resumes it, and every gate node shows its gate. Human
decisions on gates and send-backs go through :meth:`act`. The core's state is persisted as its
inputs (``control/workflow.json``) and rebuilt by replaying them: the core is pure, so the
replay reproduces it exactly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import governance, guards, risk
from ..flow import (
    CHANGE,
    ActionError,
    Completed,
    EndChange,
    Escalated,
    Evaluate,
    FlowState,
    Gate,
    Parallel,
    Preconditions,
    Record,
    ResumeStep,
    RetryStep,
    Stage,
    StartStep,
    Step,
    StopStep,
    walk,
)
from ..flow.states import GateStatus, StepStatus
from ..model import (
    ACCEPTED,
    AWAITING_HUMAN,
    BUILD,
    ESCALATED,
    FAILED,
    H_CHANGE,
    HUMAN,
    MERGING,
    PENDING,
    READY,
    RETRO,
    ChangeStatus,
    Feedback,
    Node,
)
from . import cleanup
from .core import ChangeCore
from .node_states import NodeStates
from .workflow_tree import (
    NODES,
    SCAFFOLD,
    TASKS,
    base_id,
    build_workflow,
    in_round,
    requirements_workflow,
    round_of,
    stage_title,
    task_steps,
)

if TYPE_CHECKING:
    from .runs import RunLifecycle
    from .steps import StepRunner

RECORD = "control/workflow.json"
# the ship gates H-3's own approval satisfies: not preconditions of approving it
APPROVAL_GATES = {"explicit_human_approval", "whole_change_acceptance_evidence"}


class FlowRuntime:
    def __init__(self, core: ChangeCore, runner: StepRunner, lifecycle: RunLifecycle):
        self.c, self.runner, self.lifecycle = core, runner, lifecycle
        self._cleaning = False                       # the end-of-change cleanup was started
        self.flow = FlowState(requirements_workflow())
        self.tree: dict[str, Any] | None = None       # rank, environment, blank_repo (after H-1)
        self.inputs: list[list[Any]] = []             # every input to the core, in order
        self.gates = NodeStates()            # the gate nodes are the runtime's: they follow gates
        self._feedback: dict[str, tuple[str, list[str]]] = {}   # text -> (source, docs)

    # ------------------------------------------------------------------ persistence
    def to_json(self) -> dict[str, Any]:
        return {"tree": self.tree, "inputs": self.inputs}

    def save(self) -> None:
        self.c.st._write("workflow.json", self.to_json())

    def load(self) -> None:
        """Rebuild the core from its record (its effects were carried out before)."""
        data = self.c.st._read(self.c.st.root + "workflow.json")
        self.tree = data["tree"]
        self.flow = self._new_flow()
        for inp in data["inputs"]:
            self._apply(inp, replay=True)
        self.inputs = data["inputs"]
        self._materialize()             # the nodes in the tree's order, with its dependencies

    def _new_flow(self) -> FlowState:
        wf = (requirements_workflow() if self.tree is None
              else build_workflow(self.tree["rank"], environment=self.tree["environment"],
                                  blank_repo=self.tree["blank_repo"]))
        return FlowState(wf)

    def _apply(self, inp: list[Any], *, replay: bool = False) -> list:
        """One input to the core; returns its effects. ``replay``: rebuilding the core (its
        effects were carried out then)."""
        op = inp[0]
        try:
            if op == "start":
                return self.flow.start()
            if op == "completed":
                return self.flow.on(Completed(inp[1]))
            if op == "escalated":
                return self.flow.on(Escalated(inp[1], inp[2], inp[3]))
            if op == "preconditions":
                return self.flow.on(Preconditions(inp[1], tuple(inp[2])))
            if op == "act":
                return self.flow.act(inp[1], inp[2], by=inp[3], text=inp[4],
                                     subjects=inp[5])
            if op == "redo":
                return self.flow.redo(inp[1], inp[2])
            if op == "define":
                return self.flow.define(inp[1], [Step(s["id"], overridable=s["overridable"],
                                                      depends_on=s["depends_on"])
                                                 for s in inp[2]])
        except (ActionError, KeyError, AssertionError):
            # a rebuilt tree (another tier) may not have what an earlier input was about:
            # everything after the requirements was reset by then anyway
            if not replay:
                raise
            return []
        raise ValueError(f"unknown workflow input {op!r}")

    def _input(self, *inp: Any) -> list:
        """A new input: applied, and recorded for the replay; the gates it moved show on their
        nodes."""
        mark = len(self.flow.log)
        effects = self._apply(list(inp))
        self._follow_gates(self.flow.log[mark:])
        self.inputs.append(list(inp))
        return effects

    # ------------------------------------------------------------------ starting
    def start(self) -> None:
        """A new change: its requirements stage starts."""
        self._materialize()
        self._carry_out(self._input("start"))

    def build_tree(self) -> None:
        """H-1 is being approved: the tier, environment and repository decide the rest of the
        tree. The core is rebuilt for it and replays every input so far (so attempts, approvals
        and send-backs are as they were; what the new tree lacks is skipped)."""
        ch = self.c.change
        self.tree = {"rank": risk.rank(ch.tier), "environment": self.c.cfg.environment is not None,
                     "blank_repo": ch.blank_repo}
        inputs, self.inputs = self.inputs, []
        self.flow = self._new_flow()
        for inp in inputs:
            self._apply(inp, replay=True)
        self.inputs = inputs
        self._materialize()

    # ------------------------------------------------------------------ queries
    def has(self, nid: str) -> bool:
        return nid in self.flow.nodes

    def status(self, nid: str) -> str | None:
        return self.flow.status(nid) if nid in self.flow.state else None

    def actions(self, nid: str) -> list[str]:
        return self.flow.actions(nid) if nid in self.flow.nodes or nid == CHANGE else []

    def built_on(self, nid: str) -> list[str]:
        return self.flow.built_on(nid)

    # ------------------------------------------------------------------ the engine's nodes
    def _materialize(self) -> None:
        """An engine node for every step and every gate a human sees (a gate that is not
        automatic, or has preconditions), in the tree's order, each depending (for the task
        graph shown) on what runs just before it; nodes the tree dropped (a round, an earlier
        task graph) go."""
        c, wanted = self.c, []

        def visible(n) -> bool:
            return isinstance(n, Step) or (isinstance(n, Gate) and (not n.automatic or n.preconditions))

        def place(nid: str, deps: list[str]) -> None:
            wanted.append(nid)
            if nid not in c.nodes:
                self._create(nid, deps)
            elif not nid.startswith("build:") or not c.nodes[nid].depends_on:
                c.nodes[nid].depends_on = deps

        def run(node, before: list[str]) -> list[str]:
            """Place ``node``'s nodes after ``before``; returns what comes last in it."""
            if isinstance(node, Stage):
                for child in node.children:
                    before = run(child, before)
                return run(node.gate, before)
            if isinstance(node, Parallel):
                ids = {st.id for st in node.steps}
                for st in node.steps:
                    inside = [d for d in st.depends_on if d in ids]
                    place(st.id, inside or before)
                    if st.id in c.nodes and not inside:      # a task with no task before it
                        c.nodes[st.id].depends_on = list(dict.fromkeys(
                            [*before, *c.nodes[st.id].depends_on]))
                used = {d for st in node.steps for d in st.depends_on}
                ends = [st.id for st in node.steps if st.id not in used]
                return ends or before
            if visible(node):
                place(node.id, before)
                return [node.id]
            return before

        before: list[str] = []
        for stage in self.flow.workflow.stages:
            before = run(stage, before)
        for nid in [k for k, n in c.nodes.items() if k not in self.flow.nodes and n.kind != RETRO]:
            del c.nodes[nid]
        order = wanted + [k for k in c.nodes if k not in wanted]
        for k in order:
            if k in c.nodes:
                c.nodes[k] = c.nodes.pop(k)

    def stages(self) -> list[dict[str, Any]]:
        """The stages (outermost first) with the nodes shown in each, for the task graph."""
        out: list[dict[str, Any]] = []

        def nodes_in(st: Stage) -> list[str]:
            return [n.id for n in walk(st) if n.id in self.c.nodes]

        def level(st: Stage) -> int:
            inner = [level(ch) for ch in st.children if isinstance(ch, Stage)]
            return 1 + max(inner) if inner else 0

        def visit(st: Stage, depth: int) -> None:
            out.append({"id": st.id, "title": stage_title(st.id), "depth": depth,
                        "level": level(st), "gate": st.gate.id, "nodes": nodes_in(st),
                        "status": self.status(st.id)})
            for ch in st.children:
                if isinstance(ch, Stage):
                    visit(ch, depth + 1)
        for st in self.flow.workflow.stages:
            visit(st, 0)
        return [x for x in out if x["nodes"]]

    def _create(self, nid: str, deps: list[str]) -> None:
        if nid.startswith("build:"):
            return                                   # made with its task (define_tasks)
        kind, title = NODES[base_id(nid)]
        n = round_of(nid)
        self.c.add_node(Node(id=nid, kind=kind, title=title + (f" (round {n})" if n > 1 else ""),
                             round=n, depends_on=list(deps)))

    def define_tasks(self, plan: Node) -> list[str]:
        """The critic passed the plan: its task graph's build nodes, as the steps of the tasks
        group (a blank repository's scaffold first, in its own group; a fix round: its new tasks
        only)."""
        c, n = self.c, plan.round
        taskgraph = c.json_doc("plan/taskgraph.json")
        built = {b.task.id for b in c.build_nodes() if b.round < n} if n > 1 else set()
        scaffold, tasks = task_steps(taskgraph, built=built)
        if n > 1 or SCAFFOLD not in self.flow.nodes:
            tasks, scaffold = scaffold + tasks, []
        by_id = {t["id"]: t for t in taskgraph["tasks"]}
        waves = guards.waves([dict(t, depends_on=[d for d in t["depends_on"] if d not in built])
                              for t in taskgraph["tasks"] if t["id"] not in built])
        base = max((b.wave for b in c.build_nodes() if b.round < n), default=0)
        for st in scaffold + tasks:
            t = by_id[st.id.removeprefix("build:")]
            c.nodes.pop(st.id, None)
            c.add_node(Node(id=st.id, kind=BUILD, title=f"{t['id']}: {t['title']}", task=t,
                            wave=base + waves[t["id"]], round=n,
                            depends_on=[f"build:{d}" for d in t["depends_on"]]))
        effects = []
        if scaffold:
            effects += self._input("define", SCAFFOLD, [_step_json(s) for s in scaffold])
        effects += self._input("define", in_round(TASKS, n), [_step_json(s) for s in tasks])
        self._materialize()
        self._carry_out(effects)
        return [s.id for s in scaffold + tasks]

    # ------------------------------------------------------------------ human decisions
    async def act(self, target: str, action: str, by: str, text: str = "",
                  subjects: list[str] | None = None, feedback_doc: str | None = None) -> None:
        """A generic action (approve, send_back:<stage>, evaluate, reject, abandon, …)
        through the core: what it resets is undone in the engine, what it starts is started."""
        if action.startswith("send_back:") and text:
            self._feedback[text] = ("feedback", [feedback_doc] if feedback_doc else [])
        mark = len(self.flow.log)
        effects = self._input("act", target, action, by, text, subjects)
        if self.flow.ended:                 # abandoned or rejected: nothing starts over
            self._carry_out([e for e in effects if isinstance(e, EndChange)])
            return
        self._materialize()
        # what the action reset from work done or under way (a stage's first step is started
        # again at once, so it is never seen pending afterwards)
        reset = list(dict.fromkeys(nid for nid, transition, was, _ in self.flow.log[mark:]
                                   if transition == "reset" and was != "pending"))
        await self._undo(reset)
        self._carry_out(effects)

    async def _undo(self, reset: list[str]) -> None:
        """Steps (and gates) the core sent back to pending: their nodes start over (the step
        runner's), and what their step type says a reset undoes is undone (once per policy)."""
        for policy, node in self._reset(reset):
            await policy(self.c, self.lifecycle, node)

    def _reset(self, reset: list[str]) -> list:
        """Reset the steps' nodes; returns the step types' reset policies still to run."""
        from .steps import step_for
        done: set[type] = set()
        todo = []
        for nid in reset:
            node = self.c.nodes.get(nid)
            if node is None or node.kind == HUMAN:
                continue                         # a gate node follows its gate
            self.runner.reset(node)
            for policy in step_for(node).on_reset:
                if type(policy) not in done and policy.applies(self.c, node):
                    done.add(type(policy))
                    todo.append((policy, node))
        return todo

    # ------------------------------------------------------------------ effects
    def _carry_out(self, effects: list) -> None:
        for e in effects:
            if isinstance(e, StartStep):
                self._start_step(e)
            elif isinstance(e, StopStep):
                node = self.c.nodes.get(e.step)
                if node is not None and node.status != PENDING:
                    self.runner.reset(node)
            elif isinstance(e, ResumeStep):
                node = self.c.nodes.get(e.step)
                if node is not None and node.status == ESCALATED:
                    if e.override:                 # it finishes its work without its checks
                        self.runner.resume(node, MERGING)
                    else:
                        self.runner.resume_escalation(node)
            elif isinstance(e, RetryStep):
                node = self.c.nodes.get(e.step)
                if node is not None and node.status == ESCALATED:
                    self.runner.retry(node)
            elif isinstance(e, Evaluate):
                if base_id(e.gate) == H_CHANGE:      # the ship gates read git: in the background
                    self.c.spawn(self.evaluate(e.gate))
                else:                                 # the others are the state's: at once
                    self._carry_out(self._input("preconditions", e.gate,
                                                list(self._unmet_now(e.gate))))
            elif isinstance(e, Record):
                if e.kind == "approval" and e.by == "controller":
                    self.c.event("gate", f"{e.node} approved automatically", e.node)
            elif isinstance(e, EndChange):
                self._end(e.status)

    def _start_step(self, e: StartStep) -> None:
        """The core started a step: the step runner starts its node, with the feedback it was
        sent back with."""
        node = self.c.nodes.get(e.step)
        if node is None:
            return
        feedback = []
        for text in e.feedback:
            source, atts = self._feedback.pop(text, ("feedback", []))
            feedback.append(Feedback(cycle=node.cycle, source=source, text=text,
                                     attestations=atts))
        self.runner.start(node, feedback)

    # ------------------------------------------------------------------ from the step runner
    def completed(self, node: Node) -> None:
        """A step's node is done (its last phase passed, or a round's step stands)."""
        if self.status(node.id) == StepStatus.RUNNING:
            self._carry_out(self._input("completed", node.id))

    def escalated(self, node: Node) -> None:
        if self.status(node.id) == StepStatus.RUNNING:
            esc = node.escalation
            self._carry_out(self._input("escalated", node.id, esc.kind if esc else "escalated",
                                        esc.reason if esc else ""))

    def resumed(self, node: Node) -> None:
        """The node's escalation was resolved (the core already knows when it decided it): the
        core's step goes on."""
        if self.status(node.id) == StepStatus.ESCALATED:
            self._input("act", node.id, "retry", "controller", "", None)

    # ------------------------------------------------------------------ a human's decision
    async def decide(self, node: Node, action: str, by: str, text: str = "") -> None:
        """A human's answer, retry or override on an escalated step goes to the core, whose
        effects resume the step (``ResumeStep``, ``RetryStep``). A node outside the tree (the
        retrospective) has no step there: its runner is told directly."""
        if node.id in self.flow.nodes:
            await self.act(node.id, action, by, text)
        elif action == "retry":
            self.runner.retry(node)
        elif action == "override":
            self.runner.resume(node, MERGING)
        else:
            self.runner.resume_escalation(node)

    def redo(self, step: str, feedback: Feedback) -> None:
        """A later step found ``step`` wanting: it and everything after it in its stage start
        again, it with the feedback (through the core)."""
        self._feedback[feedback.text] = (feedback.source, list(feedback.attestations))
        mark = len(self.flow.log)
        effects = self._input("redo", step, feedback.text)
        reset = list(dict.fromkeys(nid for nid, transition, was, _ in self.flow.log[mark:]
                                   if transition == "reset" and was != "pending"))
        for policy, node in self._reset(reset):
            self.c.spawn(policy(self.c, self.lifecycle, node))
        self._carry_out(effects)

    def end_at(self, node: Node) -> None:
        """The change ended at this node (abandoned at its escalation, rejected at H-3)."""
        if node.kind == HUMAN:
            self.gates.end(node)
        else:
            self.runner.end(node)

    async def evaluate(self, gate: str) -> None:
        """A gate's preconditions, evaluated now: H-3's are the ship gates (§6); H-2's (R3)
        the independent baseline's attestation."""
        c = self.c
        if base_id(gate) == H_CHANGE:
            from ..ship import evaluate
            result = await evaluate(c)
            async with c.lock:
                if self.status(gate) != GateStatus.WAITING:
                    return
                result.attestation = c.write_record("control/ship/readiness.json",
                                                    result).attestation
                c.change.ship = result
                approved = H_CHANGE in c.change.approvals
                unmet = tuple(g for g, r in result.gates.items() if r.status != "satisfied"
                              and (approved or g not in APPROVAL_GATES))
                c.event("ship", f"readiness: {result.verdict}"
                        + (f" ({len(unmet)} gate(s) unmet)" if unmet else ""), gate)
                self._carry_out(self._input("preconditions", gate, list(unmet)))
                c.save()
            return
        async with c.lock:
            if self.status(gate) != GateStatus.WAITING:
                return
            self._carry_out(self._input("preconditions", gate, list(self._unmet_now(gate))))
            c.save()

    def _unmet_now(self, gate: str) -> tuple[str, ...]:
        """H-2's precondition (R3): the independent baseline's attestation."""
        base = governance.baseline_subject(self.c)
        return (governance.BASELINE_SUBJECT,) if base is not None and base.status != "approved" \
            else ()

    def _end(self, status: str) -> None:
        c = self.c
        if status == "completed":
            c.change.status = ChangeStatus.READY
            c.event("change", "every stage is done: the change is accepted and ready")
        elif c.change.status == ChangeStatus.ACTIVE:
            c.change.status = ChangeStatus.ABANDONED

    # ------------------------------------------------------------------ on a save
    def housekeeping(self) -> None:
        """What follows from the change's state, on each save (no clock): an ended change's
        retrospective queued, its cleanup once that is done, and the step layer's housekeeping
        (:meth:`.StepRunner.housekeeping`)."""
        c = self.c
        if c.change is None:
            return
        active = c.change.status == ChangeStatus.ACTIVE
        if not active and c.cfg.retrospective.auto and RETRO not in c.nodes:
            self.add_retrospective()
            c.event("retrospective", "the change has ended: retrospective queued", RETRO)
        if not self._cleaning and cleanup.due(c):     # ended, retrospective done (§5.14)
            self._cleaning = True
            c.spawn(self._clean_up())
        self.runner.housekeeping(active)

    async def _clean_up(self) -> None:
        try:
            await cleanup.end_of_change(self.c)
        finally:
            self._cleaning = False                    # due no more once it is done

    def add_retrospective(self) -> Node:
        """Queue the retrospective of an ended change – a node outside the tree: a new one, or
        a new cycle of it."""
        c = self.c
        node = c.nodes.get(RETRO)
        if node is None:
            node = Node(id=RETRO, kind=RETRO, status=READY,
                        title="Retrospective: lessons for the assets and process")
            c.add_node(node)
            self.runner.enter(node)
        else:
            node.cycle += 1
            self.runner.again(node)
        return node

    # ------------------------------------------------------------------ gates and persistence
    _GATE = {GateStatus.PENDING: PENDING, GateStatus.WAITING: AWAITING_HUMAN,
             GateStatus.DONE: ACCEPTED}

    def _follow_gates(self, log: list) -> None:
        """Each gate node takes its gate's new status (from the core's transition log)."""
        for nid, _, _, to in log:
            node = self.c.nodes.get(nid)
            if node is None or node.kind != HUMAN or to not in self._GATE:
                continue
            if node.status == FAILED:
                continue                         # the change ended at it
            want = self._GATE[to]
            if node.status != want:
                self.gates.gate(node, want)
                if want == AWAITING_HUMAN:
                    self.c.event("human", "awaiting human decision", nid)

    def persist(self) -> None:
        """Before a save: housekeeping, then the core's record."""
        if self.c.change is not None:
            self.housekeeping()
            self.save()

    def evaluating(self) -> list[str]:
        """Gates whose preconditions were being evaluated (again after a restart)."""
        return [nid for nid, st in self.flow.state.items()
                if isinstance(self.flow.nodes.get(nid), Gate) and st.status == GateStatus.WAITING
                and tuple(getattr(st, "unmet", None) or ()) == ("(being evaluated)",)]

    def unmet(self, gate: str) -> tuple[str, ...]:
        st = self.flow.state.get(gate)
        return tuple(st.unmet or ()) if st is not None and hasattr(st, "unmet") else ()

    def approvals(self, gate: str) -> dict[str, str]:
        st = self.flow.state.get(gate)
        return dict(getattr(st, "approvals", {}) or {})


def _step_json(s: Step) -> dict[str, Any]:
    return {"id": s.id, "overridable": s.overridable, "depends_on": list(s.depends_on)}
