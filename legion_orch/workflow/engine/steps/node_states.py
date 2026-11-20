"""Node states (plan: state-model.md): the one writer of a node's status.

A node's status is the detail inside its step's status in the workflow core. The core's step
machine is coarse (``pending → running → done``, ``escalated``); the node's says where in
``running`` the work is:

    core pending    PENDING
    core running    READY · RUNNING · VERIFYING · REVIEWING · MERGING · REWORK
    core escalated  ESCALATED (something went wrong) · AWAITING_HUMAN (a decision is due: a gate)
    core done       ACCEPTED

Every status change goes through a :class:`NodeStates`, as a declared transition (an illegal
one raises). It belongs to the step runner, which tells the workflow runtime when a node crosses
one of its step's states – done, escalated, resumed. A gate is a step like any other. Nothing
else writes a status.
"""
from __future__ import annotations

from ...model import (
    ACCEPTED,
    AWAITING_HUMAN,
    ESCALATED,
    FAILED,
    MERGING,
    PENDING,
    READY,
    REVIEWING,
    REWORK,
    RUNNING,
    VERIFYING,
    Escalation,
    FailureSource,
    Node,
    NodeStatus,
    Resume,
)

#: the node statuses inside the core's ``running``: one per phase's work
IN_STEP = frozenset({READY, RUNNING, VERIFYING, REVIEWING, MERGING, REWORK})

#: the node statuses inside the core's ``escalated``: a human decides
WAITING = frozenset({ESCALATED, AWAITING_HUMAN})

#: the declared transitions (status -> the statuses it may become)
TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    PENDING: IN_STEP | {ACCEPTED},                          # entered; a round's unchanged step
    **{s: IN_STEP | {ACCEPTED, ESCALATED, AWAITING_HUMAN, PENDING} for s in IN_STEP},
    ESCALATED: IN_STEP | {PENDING, FAILED},                 # resumed, sent back, abandoned at
    AWAITING_HUMAN: IN_STEP | {PENDING, FAILED},            # approved (it checks again), reset, rejected
    ACCEPTED: frozenset({PENDING, READY}),                  # reset; the retrospective again
    FAILED: frozenset({PENDING, READY}),
}


class IllegalNodeTransition(RuntimeError):
    pass


class NodeStates:
    def _move(self, node: Node, to: NodeStatus) -> None:
        if node.status == to:
            return
        if to not in TRANSITIONS[node.status]:
            raise IllegalNodeTransition(f"{node.id}: {node.status} -> {to} is not a transition")
        node.status = to

    # ------------------------------------------------------------------ within a step
    def phase(self, node: Node, to: NodeStatus) -> None:
        """The node enters a phase, or moves between phases, of its running step (or a pending
        step is entered)."""
        if to not in IN_STEP:
            raise IllegalNodeTransition(f"{node.id}: {to} is not a phase's status")
        if node.status in WAITING:               # only resolving the escalation leaves it
            raise IllegalNodeTransition(f"{node.id}: {node.status} – resume it instead")
        self._move(node, to)

    # ------------------------------------------------------------------ the boundaries
    def accept(self, node: Node) -> None:
        """The step's work is done: its last phase passed (or a round's step stands)."""
        self._move(node, ACCEPTED)

    def escalate(self, node: Node, kind: str, reason: str, resume: Resume, *,
                 run: str | None = None, missing_decision: str | None = None,
                 log_tail: str | None = None, source: FailureSource | None = None,
                 expected: bool = False) -> None:
        """A human decides; ``resume`` says where the step continues once they have.
        ``expected``: a decision that is due, not a failure (a gate's approval):
        ``AWAITING_HUMAN`` instead of ``ESCALATED``."""
        self._move(node, AWAITING_HUMAN if expected else ESCALATED)
        node.escalation = Escalation(kind=kind, reason=reason, resume=resume, run=run,
                                     missing_decision=missing_decision, log_tail=log_tail,
                                     source=source)

    def resume(self, node: Node, to: NodeStatus) -> None:
        """The escalation is resolved: the step goes on, in the phase ``to``."""
        if node.status not in WAITING or to not in IN_STEP:
            raise IllegalNodeTransition(f"{node.id}: cannot resume {node.status} into {to}")
        node.escalation = None
        self._move(node, to)

    # ------------------------------------------------------------------ the core's decisions
    def reset(self, node: Node) -> None:
        """The core sent the step back (or stopped it): pending again."""
        node.escalation = None
        self._move(node, PENDING)

    def end(self, node: Node) -> None:
        """The change ended (abandoned or rejected) at this node."""
        self._move(node, FAILED)

    def again(self, node: Node) -> None:
        """A node outside the workflow's tree runs again (the retrospective, asked for anew)."""
        node.escalation = None
        self._move(node, READY)
