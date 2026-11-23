"""Phase spans and waits: NodeStates tells an observer of each status change, and the step
runner turns those into NodeMoves for the change's trace."""
from __future__ import annotations

import pytest
from engine_harness import REPO, Flow

from legion_orch.telemetry import NodeMove, NullTrace
from legion_orch.workflow.engine.steps.node_states import IllegalNodeTransition, NodeStates
from legion_orch.workflow.model import (
    ACCEPTED,
    AWAITING_HUMAN,
    BUILD,
    ESCALATED,
    FAILED,
    PENDING,
    READY,
    REWORK,
    RUNNING,
    Node,
    Resume,
)


def n(status=PENDING) -> Node:
    return Node(id="T1", kind=BUILD, title="t", status=status)


def observed():
    calls = []
    states = NodeStates(lambda node, before, to: calls.append(
        (before, to, node.escalation)))
    return states, calls


def test_bare_node_states_works():
    node = n()
    NodeStates().phase(node, READY)
    assert node.status == READY


def test_observer_sees_phase_accept_and_no_noop():
    states, calls = observed()
    node = n()
    states.phase(node, READY)
    states.phase(node, RUNNING)
    states.phase(node, RUNNING)               # unchanged: not reported
    states.accept(node)
    assert [(b, t) for b, t, _ in calls] == [(PENDING, READY), (READY, RUNNING), (RUNNING, ACCEPTED)]


@pytest.mark.parametrize("expected,target", [(False, ESCALATED), (True, AWAITING_HUMAN)])
def test_observer_sees_escalation_set(expected, target):
    states, calls = observed()
    node = n(RUNNING)
    states.escalate(node, "kind", "why", Resume.READY, expected=expected)
    assert calls[0][:2] == (RUNNING, target)
    assert calls[0][2] is not None and calls[0][2].kind == "kind"


def test_observer_resume_reset_end_again():
    states, calls = observed()
    node = n(RUNNING)
    states.escalate(node, "k", "r", Resume.READY)
    states.resume(node, REWORK)
    states.reset(node)
    states.phase(node, READY)
    states.escalate(node, "k", "r", Resume.READY)
    states.end(node)
    states.again(node)
    assert [(b, t) for b, t, _ in calls] == [
        (RUNNING, ESCALATED), (ESCALATED, REWORK), (REWORK, PENDING), (PENDING, READY),
        (READY, ESCALATED), (ESCALATED, FAILED), (FAILED, READY)]
    assert calls[1][2] is None                 # resumed: escalation cleared before the move


def test_illegal_escalate_leaves_node_untouched():
    states, calls = observed()
    node = n(PENDING)
    with pytest.raises(IllegalNodeTransition):
        states.escalate(node, "k", "r", Resume.READY)
    assert node.escalation is None and node.status == PENDING and calls == []


# ---------------------------------------------------------------------------- the runner
class Rec(NullTrace):
    def __init__(self):
        self.moves: list[NodeMove] = []

    def node(self, move):
        self.moves.append(move)


class StubTelemetry:
    enabled = True

    def __init__(self):
        self.rec = Rec()

    def change(self, cid):
        return self.rec

    def shutdown(self):
        return None


@pytest.mark.anyio
async def test_runner_reports_moves(tmp_path):
    f = Flow(tmp_path)
    tel = StubTelemetry()
    f.ctl.svc.telemetry = tel
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change("Say hello", REPO)
    await f.settle()
    await f.intake()
    moves = tel.rec.moves
    mine = [m for m in moves if m.node_id == "intake"]
    i = next(i for i, m in enumerate(mine) if m.to == "READY")
    assert (mine[i].phase, mine[i].order) == ("Produce", 0)
    j = next(j for j, m in enumerate(mine) if j > i and m.to == "RUNNING")
    assert mine[j].phase == "Produce"
    k = next(k for k, m in enumerate(mine) if k > j and m.to == "ACCEPTED")
    assert mine[k].phase is None

    h = [m for m in moves if m.node_id == "H-1" and m.to == "AWAITING_HUMAN"]
    assert h and h[0].expected is True and h[0].phase is None
    assert isinstance(h[0].escalation_kind, str) and h[0].escalation_kind

    await f.human("H-1", "accept")
    assert any(m.node_id == "H-1" and m.to == "ACCEPTED" for m in moves)

    runner = f.ce._runner
    before = len(moves)
    runner.adopted(f.ce.nodes["H-1"])        # accepted: nothing
    assert len(moves) == before
    live = next(node for node in f.ce.nodes.values()
                if node.status not in (PENDING, ACCEPTED, FAILED))
    runner.adopted(live)
    assert len(moves) == before + 1
    assert moves[-1].before is None and moves[-1].to == str(live.status)
