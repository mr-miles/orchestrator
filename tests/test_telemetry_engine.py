"""The traced engine end to end, in process: the spans of stages, steps, phases, rework, waits,
an abandon at an escalation, a restart, a failing exporter, and what never reaches a span."""
from __future__ import annotations

import asyncio
import time

import pytest
from engine_harness import REPO, Flow

from legion_orch import telemetry
from legion_orch.telemetry import NullTrace, Telemetry, trace_id_for
from legion_orch.workflow.engine import Controller
from legion_orch.workflow.model import ChangeStatus

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402


async def traced(tmp_path, exporter=None, goal="Say hello") -> Flow:
    f = Flow(tmp_path)
    f.ctl.svc.telemetry = Telemetry.for_exporter(exporter or InMemorySpanExporter())
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change(goal, REPO)
    await f.settle()
    return f


async def r1(f: Flow, after_intake=None, **intake) -> None:
    await f.intake(**intake)
    if after_intake:
        await f.settle()
        after_intake()
    await f.human("H-1", "accept")
    await f.plan()
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    await f.implement()
    await f.review(verdict="PASS")


def named(spans, name: str) -> list:
    return sorted((s for s in spans if s.name == name), key=lambda s: s.start_time)


def one(spans, name: str):
    found = named(spans, name)
    assert len(found) == 1, (name, [s.name for s in spans])
    return found[0]


def attr(span, key: str):
    return span.attributes.get(key)


def run(coro):
    asyncio.run(coro)


def test_r1_route_tree(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp)

        def h1_open():                       # a gate's step is open until the human acts
            assert not named(exp.get_finished_spans(), "step H-1")
        await r1(f, after_intake=h1_open)
        await f.settle()
        assert f.ce.change.status == ChangeStatus.READY
        spans = exp.get_finished_spans()
        assert {s.context.trace_id for s in spans} == {trace_id_for(f.ce.cid)}
        by_id = {s.context.span_id: s for s in spans}
        root = one(spans, f"change {f.ce.cid}")
        assert root.parent is None and attr(root, "legion.outcome") == "completed"
        flow = f.ce.workflow.flow
        for s in spans:
            if s.name.startswith("stage "):
                p = by_id[s.parent.span_id]
                assert p is root or p.name.startswith("stage ")
                assert attr(s, "legion.outcome") == "accepted"
            elif s.name.startswith("step "):
                nid = s.name[len("step "):]
                p = by_id[s.parent.span_id].attributes
                assert p.get("legion.stage.id", p.get("legion.group.id")) == flow.parent[nid].id
                assert attr(s, "legion.node.kind") == str(f.ce.nodes[nid].kind)
                assert attr(s, "legion.outcome") == "accepted"
                assert s.status.status_code != StatusCode.ERROR
            elif s.name.startswith("phase "):
                assert by_id[s.parent.span_id].name.startswith("step ")
                assert attr(s, "legion.result") in ("ok", "escalate")
            elif s.name.startswith("agent "):
                assert by_id[s.parent.span_id].name.startswith("step ")
                assert s.status.status_code != StatusCode.ERROR
        assert {"step intake", "step H-1", "step plan", "step H-E1", "step build:T1"} <= {
            s.name for s in spans}
    run(go())


def test_off_by_default(tmp_path):
    async def go():
        f = await Flow.create(tmp_path)
        assert f.ctl.svc.telemetry.enabled is False and isinstance(f.ce.trace, NullTrace)
        await r1(f)
        assert f.ce.change.status == ChangeStatus.READY
    run(go())


def test_controller_stores_telemetry(tmp_path):
    f = Flow(tmp_path)
    tel = Telemetry.for_exporter(InMemorySpanExporter())
    assert Controller(f.cfg, runner=f.runner, builder=f.builder, telemetry=tel).svc.telemetry is tel


def test_stop_interrupts_open_spans(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp)
        assert not named(exp.get_finished_spans(), f"change {f.ce.cid}")
        await f.ctl.stop()
        assert attr(one(exp.get_finished_spans(), f"change {f.ce.cid}"), "legion.outcome") == "interrupted"
    run(go())


def test_rework_phases(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp)
        await f.intake()
        await f.human("H-1", "accept")
        await f.plan()
        await f.critique("PASS")
        await f.human("H-E1", "approve")
        await f.implement()
        await f.review(verdict="FAIL")
        await f.implement()
        await f.review(verdict="PASS")
        await f.settle()
        assert f.ce.change.status == ChangeStatus.READY
        spans = exp.get_finished_spans()
        steps = named(spans, "step build:T1")
        step = steps[-1]
        phases = sorted((s for s in spans if s.name.startswith("phase ")
                         and s.parent.span_id == step.context.span_id),
                        key=lambda s: s.start_time)
        assert [s.name[len("phase "):] for s in phases] == [
            "Produce", "Verify", "Panel", "Produce", "Verify", "Panel", "Merge"]
        assert [attr(s, "legion.result") for s in phases] == [
            "ok", "ok", "rework", "ok", "ok", "ok", "ok"]
        assert len({s.context.span_id for s in phases}) == len(phases)
    run(go())


def test_gate_wait_events(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp)
        await f.intake()
        assert not named(exp.get_finished_spans(), "step H-1")
        await f.human("H-1", "accept")
        gate = one(exp.get_finished_spans(), "step H-1")
        events = {e.name: e for e in gate.events}
        assert {"awaiting_human", "resumed"} <= set(events)
        assert events["awaiting_human"].attributes["legion.expected"] is True
    run(go())


def test_send_back_ends_the_gate_and_stages_with_rework(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp)
        await f.intake()
        await f.human("H-1", "accept")
        await f.plan()
        await f.critique("PASS")
        await f.human("H-E1", "send_back", stage="plan", feedback="again")
        await f.settle()
        plans = named(exp.get_finished_spans(), "step plan")
        assert len(plans) == 1
        # the earlier step was accepted; the gate step and the containing stages carry the rework
        assert attr(plans[0], "legion.outcome") == "accepted"
        gate = one(exp.get_finished_spans(), "step H-E1")
        assert attr(gate, "legion.outcome") == "rework"
        for stage in ("stage Plan", "stage Design"):
            assert attr(one(exp.get_finished_spans(), stage), "legion.outcome") == "rework"
        await f.ctl.stop()
        plans = named(exp.get_finished_spans(), "step plan")
        assert len(plans) == 2
        assert plans[1].start_time >= plans[0].end_time
        assert attr(plans[1], "legion.outcome") == "interrupted"
    run(go())


def test_failed_agent_run(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp)
        await f.exit_agent(f.run("intake"), 1)
        await f.ctl.stop()
        agent = one(exp.get_finished_spans(), "agent intake")
        assert agent.status.status_code == StatusCode.ERROR
        assert attr(agent, "legion.outcome") == "failed"
    run(go())


def test_abandon_at_an_escalation(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp)
        r = f.run("intake")
        await f.report(r, status="BLOCKED", blocked_reason="x", missing_decision="y")
        await f.human("intake", "abandon")
        await f.settle()
        assert f.ce.change.status == ChangeStatus.ABANDONED
        spans = exp.get_finished_spans()
        # what is still open is the retrospective that follows the end, not the change itself
        assert set(f.ce.trace._open) <= {"phase:retrospective", "run:r0002"}
        step = one(spans, "step intake")
        assert attr(step, "legion.outcome") == "failed"
        assert step.status.status_code == StatusCode.ERROR
        produce = one(spans, "phase Produce")
        assert attr(produce, "legion.result") == "escalate"
        assert produce.status.status_code != StatusCode.ERROR or attr(
            produce, "legion.escalation.kind") is None
        root = one(spans, f"change {f.ce.cid}")
        assert attr(root, "legion.outcome") == "abandoned"
        assert root.status.status_code == StatusCode.ERROR
        assert not [s for s in spans if attr(s, "legion.result") == "failed"]
        assert not [s for s in spans if s.name.startswith("step ")
                    and attr(s, "legion.outcome") == "accepted" and s.end_time > step.end_time]
        await f.ctl.stop()
        assert f.ce.trace._open == {}
        assert not [s for s in exp.get_finished_spans() if attr(s, "legion.result") == "failed"]
    run(go())


def test_restart_adopts_and_finishes(tmp_path):
    async def go():
        exp1 = InMemorySpanExporter()
        f = await traced(tmp_path, exp1)
        cid = f.ce.cid
        await f.intake()
        await f.ctl.stop()
        first = exp1.get_finished_spans()
        assert {s.context.trace_id for s in first} == {trace_id_for(cid)}
        for s in first:
            if s.name == f"change {cid}" or s.name == "step H-1":
                assert attr(s, "legion.outcome") == "interrupted", s.name
            if s.name.startswith("phase "):
                assert attr(s, "legion.result") in ("ok", "interrupted", "escalate")
        assert attr(one(first, f"change {cid}"), "legion.outcome") == "interrupted"
        exp2 = InMemorySpanExporter()
        ctl2 = Controller(f.cfg, runner=f.runner, builder=f.builder,
                          telemetry=Telemetry.for_exporter(exp2))
        ctl2.load()
        async with ctl2.svc.lock:
            for ce in ctl2.changes.values():
                ce.recover()
        f.ctl, f.ce = ctl2, ctl2.changes[cid]
        await f.settle()
        await f.human("H-1", "accept")
        await f.plan()
        await f.critique("PASS")
        await f.human("H-E1", "approve")
        await f.implement()
        await f.review(verdict="PASS")
        await f.settle()
        assert f.ce.change.status == ChangeStatus.READY
        spans = exp2.get_finished_spans()
        assert {s.context.trace_id for s in spans} == {trace_id_for(cid)}
        root = one(spans, f"change {cid}")
        assert attr(root, "legion.resumed") is True
        assert attr(root, "legion.outcome") == "completed"
        assert attr(one(spans, "step H-1"), "legion.resumed") is True
        assert f.ce.trace._open == {}
    run(go())


class Raising(SpanExporter):
    def export(self, spans):
        raise RuntimeError("collector down")


class Sleeping(SpanExporter):
    def export(self, spans):
        time.sleep(2)
        return SpanExportResult.SUCCESS


def snapshot(f: Flow):
    return (f.ce.change.status, f.status(), sorted(str(r.status) for r in f.ce.runs.values()))


@pytest.mark.parametrize("exporter", [Raising, Sleeping])
def test_broken_exporter_changes_nothing(tmp_path, exporter):
    async def go():
        plain = Flow(tmp_path / "plain") if (tmp_path / "plain").mkdir() is None else None
        await plain.git.ensure()
        plain.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
        plain.ce = await plain.ctl.start_change("Say hello", REPO)
        await plain.settle()
        await r1(plain)
        (tmp_path / "traced").mkdir()
        f = Flow(tmp_path / "traced")
        f.ctl.svc.telemetry = Telemetry.for_exporter(exporter(), batch=True)
        await f.git.ensure()
        f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
        f.ce = await f.ctl.start_change("Say hello", REPO)
        await f.settle()
        await r1(f)
        assert f.ce.change.status == ChangeStatus.READY
        assert snapshot(f) == snapshot(plain)
    run(go())


def test_privacy(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp, goal="SECRET-GOAL-TEXT")
        r = f.run("intake")
        await f.report(r, status="BLOCKED", blocked_reason="SECRET-REASON",
                       missing_decision="SECRET-DECISION")
        await f.human("intake", "retry")
        await f.intake(reqs="- REQ-1: SECRET-DOC-TEXT\n")
        await f.human("H-1", "accept")
        await f.plan()
        await f.critique("PASS")
        await f.human("H-E1", "approve")
        await f.implement()
        await f.review(verdict="PASS")
        await f.ctl.stop()
        spans = exp.get_finished_spans()
        assert spans
        secrets = ("SECRET-GOAL-TEXT", "SECRET-DOC-TEXT", "SECRET-REASON")
        for s in spans:
            blobs = [s.name, str(s.status.description), *s.resource.attributes.keys(),
                     *map(str, s.resource.attributes.values())]
            for k, v in s.attributes.items():
                assert k in telemetry.ATTRS, k
                blobs += [k, str(v)]
            for e in s.events:
                blobs.append(e.name)
                for k, v in e.attributes.items():
                    assert k in telemetry.ATTRS, k
                    blobs += [k, str(v)]
            for text in blobs:
                assert not any(x in text for x in secrets), (s.name, text)
    run(go())
