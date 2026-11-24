"""The traced engine end to end, in process: a span per ticket round under its parent ticket's,
a span per agent run under its ticket's, waits and reworks as events, ends, restarts and
privacy (tests/engine_harness.py drives the change)."""
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


async def traced(tmp_path, exporter=None, goal="Say hello", *, batch=False) -> Flow:
    f = Flow(tmp_path)
    if exporter is not None:
        f.ctl.svc.telemetry = Telemetry.for_exporter(exporter, batch=batch)
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change(goal, REPO)
    await f.settle()
    return f


def traced_test(fn):
    """Run ``fn(tmp_path, exp)`` with an in-memory exporter; every controller stopped after."""
    def test(tmp_path):
        async def go():
            exp = InMemorySpanExporter()
            flows: list[Flow] = []
            try:
                await fn(tmp_path, exp, flows)
            finally:
                for f in flows:
                    await f.stop()
        asyncio.run(go())
    test.__name__ = fn.__name__
    return pytest.mark.scenario(test)


def named(spans, name: str) -> list:
    return sorted((s for s in spans if s.name == name), key=lambda s: s.start_time)


def one(spans, name: str):
    found = named(spans, name)
    assert len(found) == 1, (name, [s.name for s in spans])
    return found[0]


def attr(span, key: str):
    return span.attributes.get(key)


def parent_of(spans, span):
    return next((s for s in spans if span.parent and s.context.span_id == span.parent.span_id),
                None)


@traced_test
async def test_a_whole_change_is_one_trace_of_ticket_spans(tmp_path, exp, flows):
    f = await traced(tmp_path, exp)
    flows.append(f)
    await f.to_end()
    assert f.ce.change.status == ChangeStatus.READY
    spans = exp.get_finished_spans()
    assert {s.context.trace_id for s in spans} == {trace_id_for(f.ce.cid)}
    root = one(spans, f"change {f.ce.cid}")
    assert (attr(root, "legion.outcome"), root.parent) == ("completed", None)
    for tid, t in f.ce.st.tickets.items():         # every ticket a span, under its parent's
        if t.status == "cancelled":                # never started (the scaffold): no span
            continue
        s = one(spans, f"{t.type} {tid}")
        assert (attr(s, "legion.ticket.id"), attr(s, "legion.outcome")) == (tid, "passed")
        want = one(spans, f"{f.ce.st.tickets[t.parent].type} {t.parent}") if t.parent else root
        assert parent_of(spans, s) is want, tid
    run = one(spans, "agent planner")
    assert parent_of(spans, run).name == "agent:planner design"
    assert attr(run, "legion.outcome") == "reported"
    assert f.ce.trace._open == {}


@traced_test
async def test_a_sign_off_wait_and_a_rework_are_events(tmp_path, exp, flows):
    f = await traced(tmp_path, exp)
    flows.append(f)
    await f.to_build()
    await f.implement("T1")
    await f.review("T1", "FAIL", title="greet.txt lacks a newline")
    await f.implement("T1")
    await f.review("T1")
    await f.ctl.stop()
    spans = exp.get_finished_spans()
    h1 = one(spans, "human:signoff H-1")
    assert attr(h1, "legion.outcome") == "passed"
    t1 = one(spans, "agent:implementer T1")         # one span: the rework is in it
    events = [(e.name, e.attributes.get("legion.attempt")) for e in t1.events]
    assert [e for e in events if e[1] == 2][0] == ("working", 2), events   # straight back
    assert len(named(spans, "agent implementer")) == 2
    review = one(spans, "agent:reviewer review:T1")      # a criterion: its rounds in one span
    assert attr(review, "legion.outcome") == "passed"
    assert len(named(spans, "agent reviewer")) == 2


@traced_test
async def test_off_by_default(tmp_path, exp, flows):
    f = await traced(tmp_path)
    flows.append(f)
    assert f.ctl.svc.telemetry.enabled is False and isinstance(f.ce.trace, NullTrace)
    await f.to_end()
    assert f.ce.change.status == ChangeStatus.READY


def test_controller_stores_telemetry(tmp_path):
    f = Flow(tmp_path)
    tel = Telemetry.for_exporter(InMemorySpanExporter())
    assert Controller(f.cfg, runner=f.runner, builder=f.builder, telemetry=tel).svc.telemetry is tel


@traced_test
async def test_stop_interrupts_open_spans(tmp_path, exp, flows):
    f = await traced(tmp_path, exp)
    flows.append(f)
    assert not named(exp.get_finished_spans(), f"change {f.ce.cid}")
    await f.ctl.stop()
    spans = exp.get_finished_spans()
    assert attr(one(spans, f"change {f.ce.cid}"), "legion.outcome") == "interrupted"
    assert attr(one(spans, "agent intake"), "legion.outcome") == "interrupted"
    assert attr(one(spans, "agent:intake intake"), "legion.outcome") == "interrupted"


@traced_test
async def test_abandoning_at_an_escalation_fails_the_waiting_ticket(tmp_path, exp, flows):
    f = await traced(tmp_path, exp)
    flows.append(f)
    r = f.run("intake")
    await f.report(r, status="BLOCKED", blocked_reason="which greeting?",
                   missing_decision="the greeting")
    assert f.status("intake") == "waiting"
    await f.change_action("abandon", reason="not needed any more")
    await f.settle()
    assert f.ce.change.status == ChangeStatus.ABANDONED
    spans = exp.get_finished_spans()
    intake = one(spans, "agent:intake intake")
    assert ("waiting", "blocked") in [(e.name, e.attributes.get("legion.waiting"))
                                      for e in intake.events]
    root = one(spans, f"change {f.ce.cid}")
    assert attr(root, "legion.outcome") == "abandoned"
    assert root.status.status_code == StatusCode.ERROR
    assert f.ce.trace._open == {}


@traced_test
async def test_a_failed_agent_run_is_an_error_span(tmp_path, exp, flows):
    f = await traced(tmp_path, exp)
    flows.append(f)
    await f.exit_agent(f.run("intake"), rc=1)       # it exits without a report
    await f.settle()
    run = named(exp.get_finished_spans(), "agent intake")[0]
    assert attr(run, "legion.outcome") == "failed"
    assert run.status.status_code == StatusCode.ERROR


@traced_test
async def test_a_restart_resumes_the_trace_and_finishes(tmp_path, exp, flows):
    f = await traced(tmp_path, exp)
    flows.append(f)
    cid = f.ce.cid
    await f.intake()
    await f.ctl.stop()
    first = exp.get_finished_spans()
    assert {s.context.trace_id for s in first} == {trace_id_for(cid)}
    assert attr(one(first, f"change {cid}"), "legion.outcome") == "interrupted"
    assert attr(one(first, "agent:intake intake"), "legion.outcome") == "interrupted"  # checking
    assert attr(one(first, "agent intake"), "legion.outcome") == "reported"
    exp2 = InMemorySpanExporter()
    ctl = Controller(f.cfg, runner=f.runner, builder=f.builder,
                     telemetry=Telemetry.for_exporter(exp2))
    f.ctls.append(ctl)
    ctl.load()
    await ctl.adopt_sandboxes()
    async with ctl.svc.lock:
        for ce in ctl.changes.values():
            ce.recover()
    f.ctl, f.ce = ctl, ctl.changes[cid]
    await f.settle()
    await f.signoff("H-1")
    await f.plan()
    await f.critique("PASS")
    await f.signoff("H-E1")
    for task in [t for t, x in f.ce.st.tickets.items() if x.type == "agent:implementer"]:
        await f.implement(task)
        if f"review:{task}" in f.ce.st.tickets:
            await f.review(task)
    await f.integration()
    await f.describe()
    await f.signoff("H-3")
    assert f.ce.change.status == ChangeStatus.READY
    spans = exp2.get_finished_spans()
    assert {s.context.trace_id for s in spans} == {trace_id_for(cid)}
    root = one(spans, f"change {cid}")
    assert (attr(root, "legion.resumed"), attr(root, "legion.outcome")) == (True, "completed")
    assert attr(one(spans, "human:signoff H-1"), "legion.resumed") is True
    assert attr(one(spans, "agent:intake intake"), "legion.resumed") is True
    assert not named(spans, "agent intake")         # its run was over before the restart
    assert f.ce.trace._open == {}


class Raising(SpanExporter):
    def export(self, spans):
        raise RuntimeError("collector down")


class Sleeping(SpanExporter):
    def export(self, spans):
        time.sleep(2)
        return SpanExportResult.SUCCESS


def snapshot(f: Flow):
    return (f.ce.change.status, sorted((t, str(x.status)) for t, x in f.ce.st.tickets.items()),
            sorted(str(r.status) for r in f.ce.runs.values()))


@pytest.mark.parametrize("exporter", [Raising, Sleeping])
def test_broken_exporter_changes_nothing(tmp_path, exporter):
    async def go():
        (tmp_path / "plain").mkdir()
        (tmp_path / "traced").mkdir()
        plain = await traced(tmp_path / "plain")
        f = await traced(tmp_path / "traced", exporter(), batch=True)
        try:
            await plain.to_end()
            await f.to_end()
            assert f.ce.change.status == ChangeStatus.READY
            assert snapshot(f) == snapshot(plain)
        finally:
            await plain.stop()
            await f.stop()
    asyncio.run(go())


@traced_test
async def test_privacy(tmp_path, exp, flows):
    f = await traced(tmp_path, exp, goal="SECRET-GOAL-TEXT")
    flows.append(f)
    r = f.run("intake")
    await f.report(r, status="BLOCKED", blocked_reason="SECRET-REASON",
                   missing_decision="SECRET-DECISION")
    await f.human("intake", "retry")
    await f.intake(reqs="- REQ-1: SECRET-DOC-TEXT\n")
    await f.signoff("H-1")
    await f.plan()
    await f.ctl.stop()
    spans = exp.get_finished_spans()
    assert spans
    secrets = ("SECRET-GOAL-TEXT", "SECRET-DOC-TEXT", "SECRET-REASON", "SECRET-DECISION")
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
