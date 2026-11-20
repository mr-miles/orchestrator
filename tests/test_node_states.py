"""Node states (engine/node_states.py): the declared transitions of a node's status, nested in
its step's status in the workflow core – and the rule that nothing else writes a status."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from legion_orch.workflow.engine.node_states import (
    IN_STEP,
    TRANSITIONS,
    IllegalNodeTransition,
    NodeStates,
)
from legion_orch.workflow.model import (
    ACCEPTED,
    AWAITING_HUMAN,
    BUILD,
    ESCALATED,
    FAILED,
    MERGING,
    PENDING,
    READY,
    REWORK,
    RUNNING,
    VERIFYING,
    Node,
    NodeStatus,
    Resume,
)

ROOT = Path(__file__).resolve().parent.parent


def n(status=PENDING) -> Node:
    return Node(id="T1", kind=BUILD, title="t", status=status)


def test_every_status_has_its_transitions():
    assert set(TRANSITIONS) == set(NodeStatus)
    assert IN_STEP == {READY, RUNNING, VERIFYING, "REVIEWING", MERGING, REWORK}


def test_a_step_moves_through_its_phases_and_ends_accepted():
    s, node = NodeStates(), n()
    for to in (READY, RUNNING, VERIFYING, MERGING):
        s.phase(node, to)
    s.accept(node)
    assert node.status == ACCEPTED


def test_an_escalation_holds_the_step_until_it_is_resumed_in_a_phase():
    s, node = NodeStates(), n(RUNNING)
    s.escalate(node, "blocked", "why", Resume.REWORK, run="r1")
    assert node.status == ESCALATED and node.escalation.run == "r1"
    with pytest.raises(IllegalNodeTransition):
        s.phase(node, REWORK)                  # only resume leaves an escalation
    s.resume(node, REWORK)
    assert node.status == REWORK and node.escalation is None


@pytest.mark.parametrize("start, illegal", [
    (PENDING, ESCALATED),                      # a step not started cannot escalate
    (ACCEPTED, RUNNING),                       # a done step is not run again past the core
    (PENDING, AWAITING_HUMAN),                 # nor wait for a decision
])
def test_illegal_transitions_raise(start, illegal):
    s, node = NodeStates(), n(start)
    with pytest.raises(IllegalNodeTransition):
        s._move(node, illegal)


def test_a_due_decision_waits_like_an_escalation_and_is_resumed_the_same_way():
    """A gate step's approval: escalated in the core, AWAITING_HUMAN on its node."""
    s, node = NodeStates(), n(RUNNING)
    s.escalate(node, "awaiting_approval", "approve the plan", Resume.READY, expected=True)
    assert node.status == AWAITING_HUMAN and node.escalation.kind == "awaiting_approval"
    with pytest.raises(IllegalNodeTransition):
        s.phase(node, RUNNING)
    s.resume(node, READY)
    assert node.status == READY and node.escalation is None


def test_a_reset_returns_any_node_to_pending():
    s = NodeStates()
    for start in (READY, VERIFYING, ESCALATED, ACCEPTED, AWAITING_HUMAN, FAILED):
        node = n(start)
        s.reset(node)
        assert node.status == PENDING


def test_only_node_states_writes_a_nodes_status():
    """The engine and the role handlers change a node's status only through NodeStates."""
    wf = ROOT / "legion_orch/workflow"
    write = re.compile(r"\b(?!run\b|r\b|cur\b|planner\b|judge\b|change\b|ch\b|st\b|tr\b|res\b)"
                       r"\w+\.status\s*=[^=]")
    found = [f"{p.relative_to(ROOT)}:{i}" for p in wf.rglob("*.py")
             if p.name != "node_states.py" and "flow" not in p.parts
             for i, line in enumerate(p.read_text().splitlines(), 1) if write.search(line)]
    assert found == []
