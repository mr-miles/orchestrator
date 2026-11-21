"""The tracer wired into the engine: off by default (nothing changes); on, an R1 run exports one
trace shaped like the flow tree, and stop() ends what is still open as interrupted."""
from __future__ import annotations

import asyncio

import pytest
from engine_harness import REPO, Flow

from legion_orch.telemetry import NullTrace, Telemetry
from legion_orch.workflow.model import ChangeStatus

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402


async def traced(tmp_path, exporter=None) -> Flow:
    f = Flow(tmp_path)
    f.ctl.svc.telemetry = Telemetry.for_exporter(exporter or InMemorySpanExporter())
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change("Say hello", REPO)
    await f.settle()
    return f


async def r1(f: Flow) -> None:
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    await f.implement()
    await f.review(verdict="PASS")


def by_name(spans) -> dict:
    return {s.name: s for s in spans}


def test_off_by_default(tmp_path):
    async def go():
        f = await Flow.create(tmp_path)
        assert f.ctl.svc.telemetry.enabled is False
        assert isinstance(f.ce.trace, NullTrace)
        await r1(f)
        assert f.ce.change.status == ChangeStatus.READY
    asyncio.run(go())


def test_controller_stores_telemetry(tmp_path):
    f = Flow(tmp_path)
    tel = Telemetry.for_exporter(InMemorySpanExporter())
    from legion_orch.workflow.engine import Controller
    ctl = Controller(f.cfg, runner=f.runner, builder=f.builder, telemetry=tel)
    assert ctl.svc.telemetry is tel


def test_r1_run_exports_one_trace(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp)
        await f.intake()
        await f.settle()
        assert "step H-1" not in by_name(exp.get_finished_spans())
        await f.human("H-1", "accept")
        await f.plan()
        await f.critique("PASS")
        await f.human("H-E1", "approve")
        await f.implement()
        await f.review(verdict="PASS")
        await f.settle()
        assert f.ce.change.status == ChangeStatus.READY
        spans = exp.get_finished_spans()
        assert len({s.context.trace_id for s in spans}) == 1
        named = by_name(spans)
        root = named[f"change {f.ce.cid}"]
        assert root.attributes["legion.outcome"] == "completed"
        assert root.parent is None
        by_id = {s.context.span_id: s for s in spans}
        stages = [s for s in spans if s.name.startswith("stage ")]
        assert stages
        for s in stages:
            parent = by_id[s.parent.span_id]
            assert parent is root or parent.name.startswith("stage ")
        steps = [s for s in spans if s.name.startswith("step ")]
        flow = f.ce.workflow.flow
        for s in steps:
            nid = s.name[len("step "):]
            assert s.attributes["legion.node.kind"] == str(f.ce.nodes[nid].kind)
            par = flow.parent[nid]
            parent = by_id[s.parent.span_id]
            attrs = parent.attributes
            assert attrs.get("legion.stage.id", attrs.get("legion.group.id")) == par.id
        assert named["step H-1"].attributes["legion.outcome"] == "accepted"
        for role in ("intake", "planner", "implementer"):
            agent = named[f"agent {role}"]
            assert by_id[agent.parent.span_id].name.startswith("step ")
    asyncio.run(go())


def test_stop_interrupts_open_spans(tmp_path):
    async def go():
        exp = InMemorySpanExporter()
        f = await traced(tmp_path, exp)
        assert f"change {f.ce.cid}" not in by_name(exp.get_finished_spans())
        await f.ctl.stop()
        spans = exp.get_finished_spans()
        root = by_name(spans)[f"change {f.ce.cid}"]
        assert root.attributes["legion.outcome"] == "interrupted"
    asyncio.run(go())
