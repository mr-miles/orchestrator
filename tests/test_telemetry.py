"""legion_orch.telemetry: span tree, ends, restarts, safety and configuration (no engine)."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from legion_orch import telemetry as tel
from legion_orch.config import TelemetryConfig
from legion_orch.telemetry import NodeMove, NullTrace, Subject, Telemetry

pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace.export import SpanExporter  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

CID = "c1-0001"


class Rig:
    def __init__(self, cid: str = CID):
        self.exp = InMemorySpanExporter()
        self.t = Telemetry.for_exporter(self.exp)
        self.tr = self.t.change(cid)

    @property
    def spans(self):
        return self.exp.get_finished_spans()

    def one(self, name: str):
        found = [s for s in self.spans if s.name == name]
        assert len(found) == 1, (name, [s.name for s in self.spans])
        return found[0]

    def all(self, name: str):
        return [s for s in self.spans if s.name == name]


DESC = {
    "design": Subject("stage", None, None, "Design"),
    "g1": Subject("group", "design", None, None),
    "H-1": Subject("step", "design", "human", None),
    "H-3": Subject("step", None, "human", None),
    "intake": Subject("step", None, "build", None),
}


def describe(nid):
    return DESC.get(nid)


def mv(nid="H-1", to="RUNNING", before="READY", phase="Produce", order=0, cycle=1,
       kind="human", esc=None, expected=False):
    return NodeMove(nid, kind, cycle, before, to, phase, order, esc, expected)


def attr(span, key):
    return span.attributes.get(key)


# ---- off / construction -------------------------------------------------------------------

def test_off_is_noop():
    t = Telemetry.off()
    assert t.enabled is False
    tr = t.change(CID)
    assert isinstance(tr, NullTrace) and not isinstance(tr, tel.ChangeTrace)
    tr.begin("w")
    tr.resume([], ended=True)
    tr.flow([], describe)
    tr.node(mv())
    tr.runs([])
    tr.end("completed")
    tr.interrupt()
    t.shutdown()


def test_module_import_without_opentelemetry():
    code = ("import sys; sys.modules['opentelemetry'] = None; "
            "import legion_orch.telemetry as t; assert t.ATTRS; print('ok')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


def test_attrs_and_hint():
    assert len(tel.ATTRS) == 16
    assert tel.INSTALL_HINT in str(tel.TelemetryUnavailable())


def cfg(**kw):
    return SimpleNamespace(project="p", telemetry=TelemetryConfig(**kw))


EP = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}


@pytest.mark.parametrize("kw,env,on", [
    ({}, {}, False),
    ({}, EP, True),
    ({}, {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://x/v1/traces"}, True),
    ({}, {**EP, "OTEL_SDK_DISABLED": "TRUE"}, False),
    ({}, {**EP, "OTEL_TRACES_EXPORTER": "none"}, False),
    ({"enabled": False}, EP, False),
    ({"enabled": True}, {}, True),
    ({"enabled": True, "endpoint": "http://h:1/"}, {}, True),
])
def test_from_config_matrix(kw, env, on):
    t = Telemetry.from_config(cfg(**kw), env)
    assert t.enabled is on
    t.shutdown()


def test_unavailable_explicit_and_auto(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)
    with pytest.raises(tel.TelemetryUnavailable, match="not installed"):
        Telemetry.from_config(cfg(enabled=True), {})
    with caplog.at_level("WARNING"):
        t = Telemetry.from_config(cfg(), EP)
    assert t.enabled is False
    assert "not installed" in caplog.text


def test_trace_id_deterministic():
    a, b, c = Rig(), Rig(), Rig("other")
    for r in (a, b, c):
        r.tr.begin("w")
        r.tr.end("completed")
    assert a.one(f"change {CID}").context.trace_id == b.one(f"change {CID}").context.trace_id
    assert a.one(f"change {CID}").context.trace_id == tel.trace_id_for(CID)
    assert c.one("change other").context.trace_id != tel.trace_id_for(CID)


# ---- span tree ------------------------------------------------------------------------------

def test_span_tree():
    r = Rig()
    tr = r.tr
    tr.begin("ws")
    tr.flow([("design", "start", "pending", "running")], describe)
    tr.flow([("H-1", "start", "pending", "running")], describe)
    tr.node(mv(to="RUNNING", before="READY", phase="Produce", order=0))
    tr.node(mv(to="VERIFYING", before="RUNNING", phase="Verify", order=1))
    tr.node(mv(to="REVIEWING", before="VERIFYING", phase="Panel", order=2))
    tr.node(mv(to="RUNNING", before="REVIEWING", phase="Produce", order=0, cycle=2))
    tr.node(mv(to="VERIFYING", before="RUNNING", phase="Verify", order=1, cycle=2))
    tr.node(mv(to="ACCEPTED", before="VERIFYING", phase=None, order=None, cycle=2))
    tr.flow([("H-1", "complete", "running", "accepted")], describe)
    tr.flow([("design", "complete", "running", "accepted")], describe)
    tr.end("completed")

    root = r.one(f"change {CID}")
    stage = r.one("stage Design")
    step = r.one("step H-1")
    assert root.parent is None and attr(root, "legion.workspace") == "ws"
    assert attr(root, "legion.outcome") == "completed" and root.status.status_code == StatusCode.UNSET
    assert stage.parent.span_id == root.context.span_id
    assert step.parent.span_id == stage.context.span_id
    assert attr(stage, "legion.outcome") == "accepted" and attr(step, "legion.outcome") == "accepted"
    assert attr(step, "legion.node.kind") == "human" and attr(step, "legion.cycle") == 2
    phases = [(s.name, attr(s, "legion.result")) for s in r.spans if s.name.startswith("phase")]
    assert phases == [("phase Produce", "ok"), ("phase Verify", "ok"), ("phase Panel", "rework"),
                      ("phase Produce", "ok"), ("phase Verify", "ok")]
    assert [p for p in phases if p[0] == "phase Panel"] == [("phase Panel", "rework")]
    assert phases[-1] == ("phase Verify", "ok")
    for s in r.spans:
        assert s.context.trace_id == root.context.trace_id
        assert attr(s, "legion.change.id") == CID
        assert set(s.attributes) <= tel.ATTRS
    ph = r.all("phase Produce")[0]
    assert ph.parent.span_id == step.context.span_id and attr(ph, "legion.phase") == "Produce"


def test_status_event_in_same_phase():
    r = Rig()
    r.tr.begin("w")
    r.tr.flow([("H-1", "start", "pending", "running")], describe)
    r.tr.node(mv(to="READY", before="PENDING"))
    r.tr.node(mv(to="RUNNING", before="READY"))
    r.tr.end("completed")
    assert [e.name for e in r.one("step H-1").events] == ["status RUNNING"]


def test_group_and_removed():
    r = Rig()
    r.tr.begin("w")
    r.tr.flow([("design", "start", "p", "r"), ("g1", "start", "p", "r"),
               ("H-1", "start", "p", "r")], describe)
    r.tr.flow([("H-1", "removed", "r", "x")], lambda n: None)
    r.tr.flow([("g1", "complete", "r", "a")], describe)
    assert attr(r.one("step H-1"), "legion.outcome") == "abandoned"
    g = r.one("group g1")
    assert g.parent.span_id == r.tr._open["stage:design"].span.get_span_context().span_id
    assert attr(g, "legion.group.id") == "g1" and attr(g, "legion.outcome") == "accepted"


def test_waits_and_resume():
    r = Rig()
    r.tr.begin("w")
    r.tr.flow([("H-1", "start", "p", "r")], describe)
    r.tr.node(mv(to="RUNNING"))
    r.tr.node(mv(to="AWAITING_HUMAN", before="RUNNING", phase="Approval", order=3,
                 esc="gate", expected=True))
    assert attr(r.one("phase Produce"), "legion.result") == "escalate"
    assert r.one("phase Produce").status.status_code == StatusCode.UNSET
    r.tr.node(mv(to="RUNNING", before="AWAITING_HUMAN"))
    r.tr.flow([("H-1", "complete", "r", "a")], describe)
    ev = r.one("step H-1").events
    assert [e.name for e in ev] == ["awaiting_human", "resumed"]
    assert ev[0].attributes == {"legion.escalation.kind": "gate", "legion.expected": True}


def test_reset_rework_then_new_span():
    r = Rig()
    r.tr.begin("w")
    r.tr.flow([("H-1", "start", "p", "r")], describe)
    r.tr.node(mv(to="RUNNING"))
    r.tr.flow([("H-1", "reset", "r", "p")], describe)
    first = r.one("step H-1")
    assert attr(first, "legion.outcome") == "rework"
    assert attr(r.one("phase Produce"), "legion.result") == "sent_back"
    r.tr.flow([("H-1", "start", "p", "r")], describe)
    r.tr.end("abandoned")
    assert len(r.all("step H-1")) == 2


def test_adopt_sets_resumed():
    r = Rig()
    r.tr.resume([])
    r.tr.flow([("design", "adopt", "pending", "running"), ("H-1", "adopt", "pending", "running")],
              describe)
    r.tr.node(mv(to="RUNNING", before=None))
    r.tr.interrupt()
    root = r.one(f"change {CID}")
    assert attr(root, "legion.resumed") is True
    for n in ("stage Design", "step H-1", "phase Produce"):
        assert attr(r.one(n), "legion.resumed") is True
    assert attr(r.one("step H-1"), "legion.outcome") == "interrupted"
    assert attr(r.one("phase Produce"), "legion.result") == "interrupted"


def run(rid="r1", status="ACTIVE", node="H-1", role="dev", cycle=1):
    return SimpleNamespace(id=rid, node_id=node, role=role, cycle=cycle, status=status)


def test_runs():
    r = Rig()
    r.tr.resume(["old"])
    r.tr.flow([("H-1", "adopt", "p", "r")], describe)
    r.tr.runs([run("old"), run("r1", "STARTING"), run("r2", "REPORTED")])
    r.tr.runs([run("old", "FAILED"), run("r1", "ACTIVE"), run("r2", "ACTIVE")])
    assert not r.all("agent dev")
    r.tr.runs([run("r1", "FAILED")])
    a = r.one("agent dev")
    assert attr(a, "legion.outcome") == "failed" and a.status.status_code == StatusCode.ERROR
    assert attr(a, "legion.run.id") == "r1" and attr(a, "legion.node.id") == "H-1"
    assert a.parent.span_id == r.tr._open["step:H-1"].span.get_span_context().span_id
    r.tr.runs([run("r3", "ACTIVE")])
    r.tr.runs([run("r3", "ACCEPTED")])
    assert attr(r.all("agent dev")[1], "legion.outcome") == "accepted"


# ---- end of change -------------------------------------------------------------------------

def _ended_slice(r, nid):
    r.tr.flow([(nid, "reset", "running", "pending"), ("H-1", "reset", "r", "p")], describe,
              ended=True)
    assert not r.all(f"step {nid}")


def test_end_reject_at_gate():
    r = Rig()
    r.tr.begin("w")
    r.tr.flow([("design", "start", "p", "r"), ("H-1", "start", "p", "r"),
               ("H-3", "start", "p", "r")], describe)
    r.tr.node(mv("H-1", to="RUNNING"))
    r.tr.node(mv("H-3", to="RUNNING"))
    r.tr.runs([run("r1", "ACTIVE", "H-1")])
    r.tr.node(mv("H-3", to="AWAITING_HUMAN", before="RUNNING", phase="Approval", order=3,
                 esc="gate", expected=True))
    _ended_slice(r, "H-3")
    r.tr.end("rejected")
    h3 = r.one("step H-3")
    assert attr(h3, "legion.outcome") == "failed" and h3.status.status_code == StatusCode.ERROR
    assert h3.status.description == "rejected"
    p = [s for s in r.all("phase Produce") if attr(s, "legion.node.id") == "H-3"][0]
    assert attr(p, "legion.result") == "escalate" and p.status.status_code == StatusCode.UNSET
    h1 = r.one("step H-1")
    assert attr(h1, "legion.outcome") == "abandoned" and h1.status.status_code == StatusCode.UNSET
    assert attr(r.one("agent dev"), "legion.outcome") == "interrupted"
    assert attr(r.one("stage Design"), "legion.outcome") == "abandoned"
    root = r.one(f"change {CID}")
    assert attr(root, "legion.outcome") == "rejected" and root.status.status_code == StatusCode.ERROR
    assert not r.tr._open
    assert all(attr(s, "legion.result") != "failed" for s in r.spans)


def test_end_abandon_at_escalated_step():
    r = Rig()
    r.tr.begin("w")
    r.tr.flow([("intake", "start", "p", "r"), ("H-1", "start", "p", "r")], describe)
    r.tr.node(mv("intake", to="RUNNING", kind="build"))
    r.tr.node(mv("intake", to="ESCALATED", before="RUNNING", phase=None, order=None,
                 esc="blocked", expected=False, kind="build"))
    ph = r.one("phase Produce")
    assert attr(ph, "legion.result") == "escalate" and ph.status.status_code == StatusCode.ERROR
    assert ph.status.description == "blocked"
    r.tr.flow([("intake", "reset", "r", "p")], describe, ended=True)
    r.tr.end("abandoned")
    st = r.one("step intake")
    assert attr(st, "legion.outcome") == "failed" and st.status.status_code == StatusCode.ERROR
    assert st.status.description == "abandoned"
    assert [e.name for e in st.events] == ["escalated"]
    assert attr(r.one("step H-1"), "legion.outcome") == "abandoned"
    root = r.one(f"change {CID}")
    assert attr(root, "legion.outcome") == "abandoned" and root.status.status_code == StatusCode.ERROR
    assert not r.tr._open


def test_node_failed_closes_open_phase():
    r = Rig()
    r.tr.begin("w")
    r.tr.node(mv(to="RUNNING"))
    r.tr.node(mv(to="FAILED", before="RUNNING", phase=None, order=None))
    r.tr.node(mv(to="FAILED", before="FAILED", phase=None, order=None))
    p = r.one("phase Produce")
    assert attr(p, "legion.result") == "failed" and p.status.status_code == StatusCode.ERROR


def test_after_end_retrospective_and_restart():
    r = Rig()
    r.tr.begin("w")
    r.tr.end("completed")
    root = r.one(f"change {CID}")
    assert root.status.status_code == StatusCode.UNSET
    r.tr.node(mv("retro", to="RUNNING"))
    r.tr.runs([run("rr", "ACTIVE", "retro", "retro")])
    r.tr.interrupt()
    ph = r.one("phase Produce")
    assert ph.parent.span_id == root.context.span_id
    assert ph.context.trace_id == root.context.trace_id
    assert r.one("agent retro").parent.span_id == root.context.span_id
    assert len(r.all(f"change {CID}")) == 1

    r2 = Rig()
    r2.tr.resume([], ended=True)
    r2.tr.node(mv("retro", to="RUNNING", before=None))
    r2.tr.interrupt()
    ph2 = r2.one("phase Produce")
    assert ph2.parent is None and attr(ph2, "legion.resumed") is True
    assert ph2.context.trace_id == tel.trace_id_for(CID)
    assert not r2.all(f"change {CID}")


def test_interrupt_closes_everything():
    r = Rig()
    r.tr.begin("w")
    r.tr.flow([("design", "start", "p", "r"), ("H-1", "start", "p", "r")], describe)
    r.tr.node(mv(to="RUNNING"))
    r.tr.runs([run()])
    r.tr.interrupt()
    assert not r.tr._open
    assert len(r.spans) == 5
    assert all(s.status.status_code == StatusCode.UNSET for s in r.spans)
    assert attr(r.one(f"change {CID}"), "legion.outcome") == "interrupted"
    assert attr(r.one("agent dev"), "legion.outcome") == "interrupted"


# ---- allowlist, safety, shutdown -------------------------------------------------------------

def test_attrs_allowlist():
    out = tel._attrs(**{"legion.change.id": "x" * 300, "secret": "s", "legion.cycle": None,
                        "legion.expected": False})
    assert out == {"legion.change.id": "x" * 128, "legion.expected": False}


def test_guarded_never_raises(monkeypatch, caplog):
    r = Rig()

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(r.tr, "_make_root", boom)
    monkeypatch.setattr(r.tr, "_phase", boom)
    monkeypatch.setattr(r.tr, "_flow_one", boom)
    monkeypatch.setattr(r.tr, "_close_all", boom)
    assert r.tr.begin("w") is None
    assert r.tr.node(mv()) is None
    assert r.tr.flow([("H-1", "start", "p", "r")], describe) is None
    assert r.tr.end("completed") is None
    assert r.tr.interrupt() is None
    assert r.tr.runs([object()]) is None
    assert r.tr.resume(5) is None


def test_shutdown_flushes_and_is_idempotent():
    r = Rig()
    r.tr.begin("w")
    r.t.shutdown()
    r.t.shutdown()
    assert attr(r.one(f"change {CID}"), "legion.outcome") == "interrupted"


def test_batch_with_failing_exporter():
    class Bad(SpanExporter):
        def export(self, spans):
            raise RuntimeError("down")

        def shutdown(self):
            pass

    t = Telemetry.for_exporter(Bad(), batch=True)
    tr = t.change(CID)
    tr.begin("w")
    tr.flow([("H-1", "start", "p", "r")], describe)
    tr.end("completed")
    t.shutdown()
    t.shutdown()
