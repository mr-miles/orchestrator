"""The three node state machines (legion_orch/workflow/flow/states.py): their transition tables
(derived from the code, asserted here so that changing a machine is a visible change), illegal
transitions refused, status read-only, and the transition log."""
from __future__ import annotations

import pytest

from legion_orch.workflow.flow.states import (
    GateState,
    GateStatus,
    IllegalTransition,
    StageState,
    StageStatus,
    StepState,
    StepStatus,
)

S, G, T = StepStatus, GateStatus, StageStatus


def table(cls) -> dict[str, tuple[set[str], str]]:
    return {name: ({str(x) for x in src}, str(to)) for name, (src, to) in cls.transitions.items()}


def test_the_step_machine():
    assert table(StepState) == {
        "start": ({"pending"}, "running"),
        "rework": ({"running"}, "running"),
        "complete": ({"running"}, "done"),
        "escalate": ({"running"}, "escalated"),
        "resume": ({"escalated"}, "running"),
        "override": ({"escalated"}, "done"),
        "reset": ({"pending", "running", "escalated", "done"}, "pending"),
    }


def test_the_gate_machine():
    assert table(GateState) == {
        "open": ({"pending"}, "waiting"),
        "approve": ({"waiting"}, "waiting"),
        "evaluating": ({"waiting"}, "waiting"),
        "assessed": ({"waiting"}, "waiting"),
        "complete": ({"waiting"}, "done"),
        "reset": ({"pending", "waiting", "done"}, "pending"),
    }


def test_the_stage_machine():
    assert table(StageState) == {
        "start": ({"pending"}, "running"),
        "complete": ({"running"}, "done"),
        "reopen": ({"running", "done"}, "running"),
        "reset": ({"pending", "running", "done"}, "pending"),
    }


def test_every_status_is_reachable_and_every_target_is_a_status():
    for cls, statuses in ((StepState, S), (GateState, G), (StageState, T)):
        targets = {to for _, to in cls.transitions.values()}
        assert targets <= set(statuses)
        assert set(statuses) - {cls.initial} <= targets, cls


@pytest.mark.parametrize(("make", "call"), [
    (StepState, lambda m: m.complete()),                 # not started
    (StepState, lambda m: m.resume()),                   # not escalated
    (StepState, lambda m: m.override()),
    (GateState, lambda m: m.approve("x", "ana")),        # not waiting
    (GateState, lambda m: m.complete()),
    (StageState, lambda m: m.complete()),                # not started
    (StageState, lambda m: m.reopen()),                  # never ran
])
def test_an_illegal_transition_raises_and_changes_nothing(make, call):
    m = make()
    with pytest.raises(IllegalTransition, match="not from pending"):
        call(m)
    assert m.status == m.initial


def test_a_completed_step_cannot_start_or_escalate_again_without_a_reset():
    m = StepState()
    m.start()
    m.complete()
    for call in (m.start, m.complete, lambda: m.escalate("k", "q")):
        with pytest.raises(IllegalTransition, match="not from done"):
            call()
    m.reset()
    assert m.status == S.PENDING and m.attempt == 1


def test_status_is_read_only():
    m = StepState()
    with pytest.raises(AttributeError):
        m.status = S.DONE  # type: ignore[misc]


def test_a_start_is_a_new_attempt_carrying_its_feedback_once():
    m = StepState()
    m.feedback.append("smaller tasks")
    assert m.start() == ("smaller tasks",) and m.attempt == 1
    m.reset()
    assert m.start() == () and m.attempt == 2              # the feedback was used up


def test_escalation_is_kept_until_resumed_overridden_or_reset():
    m = StepState()
    m.start()
    m.escalate("blocked", "which timeout?")
    assert m.status == S.ESCALATED and m.escalation == ("blocked", "which timeout?")
    m.resume()
    assert m.status == S.RUNNING and m.escalation is None and m.attempt == 1
    m.escalate("blocked", "again")
    m.override()
    assert m.status == S.DONE and m.escalation is None


def test_a_gate_collects_approvals_and_preconditions_while_waiting():
    g = GateState()
    g.open()
    g.approve("plan", "ana")
    g.assessed(("ship gate",))
    assert g.status == G.WAITING and g.approvals == {"plan": "ana"} and g.unmet == ("ship gate",)
    g.evaluating()
    assert g.unmet is None
    g.complete()
    g.reset()
    assert g.status == G.PENDING and g.approvals == {} and g.unmet is None


def test_watched_machines_log_every_transition():
    log: list = []
    m = StepState()
    m.watch("T1", log)
    m.start()
    m.rework()
    m.complete()
    assert log == [("T1", "start", "pending", "running"), ("T1", "rework", "running", "running"),
                   ("T1", "complete", "running", "done")]
    with pytest.raises(IllegalTransition):
        m.complete()
    assert len(log) == 3                                    # a refused transition is not logged
