"""legion_orch.telemetry: span tree, ends, restarts, safety and configuration (no engine)."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from legion_orch import telemetry as tel
from legion_orch.config import TelemetryConfig
from legion_orch.telemetry import NullTrace, Telemetry

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


def tk(tid, status="working", parent="change", type_="agent:implementer", attempt=1,
       waiting=None):
    return SimpleNamespace(id=tid, status=status, parent=parent, type=type_, attempt=attempt,
                           waiting=waiting)


def root(status="subtasks"):
    return tk("change", status, None, "grouping")


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
    tr.tickets([])
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
    assert len(tel.ATTRS) == 11
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

def test_ticket_spans_nest_as_the_tree():
    r = Rig()
    r.tr.begin("w")
    r.tr.tickets([root(), tk("delivery", "subtasks", type_="grouping"),
                  tk("T1", parent="delivery"), tk("T2", "pending", parent="delivery")])
    r.tr.tickets([root(), tk("delivery", "subtasks", type_="grouping"),
                  tk("T1", "passed", parent="delivery"), tk("T2", "cancelled", parent="delivery")])
    t1 = r.one("agent:implementer T1")
    assert (attr(t1, "legion.ticket.id"), attr(t1, "legion.outcome")) == ("T1", "passed")
    assert t1.parent.span_id == r.tr._open["ticket:delivery"].span.get_span_context().span_id
    assert not r.all("agent:implementer T2")           # never started: no span
    r.tr.end("completed")
    names = {s.name for s in r.spans}
    assert names == {f"change {CID}", "grouping change", "grouping delivery",
                     "agent:implementer T1"}
    assert attr(r.one("grouping change"), "legion.outcome") == "abandoned"   # still open at end
    assert r.one("grouping change").parent.span_id == \
        r.one(f"change {CID}").context.span_id


def test_a_parent_span_opens_before_its_child():
    r = Rig()
    r.tr.tickets([tk("T1", parent="delivery"), root(), tk("delivery", "subtasks", type_="grouping")])
    assert list(r.tr._open) == ["ticket:change", "ticket:delivery", "ticket:T1"]
    r.tr.interrupt()
    t1 = r.one("agent:implementer T1")
    assert t1.parent.span_id == r.one("grouping delivery").context.span_id


def test_status_changes_are_events_with_the_attempt():
    r = Rig()
    r.tr.tickets([root(), tk("T1")])
    r.tr.tickets([root(), tk("T1")])                    # nothing changed: no event
    r.tr.tickets([root(), tk("T1", "checking")])
    r.tr.tickets([root(), tk("T1", "working", attempt=2)])
    r.tr.tickets([root(), tk("T1", "waiting", attempt=2, waiting="blocked")])
    r.tr.tickets([root(), tk("T1", "passed", attempt=2)])
    t1 = r.one("agent:implementer T1")
    assert [(e.name, e.attributes.get("legion.attempt")) for e in t1.events] == [
        ("working", 1), ("checking", 1), ("working", 2), ("waiting", 2)]
    assert t1.events[-1].attributes["legion.waiting"] == "blocked"
    assert attr(t1, "legion.attempt") == 2


def test_closed_in_one_save_still_has_a_span_but_not_if_closed_before_known():
    r = Rig()
    r.tr.tickets([root(), tk("G", "pending", type_="grouping")])
    r.tr.tickets([root(), tk("G", "passed", type_="grouping"), tk("old", "passed")])
    assert attr(r.one("grouping G"), "legion.outcome") == "passed"
    assert not r.all("agent:implementer old")


def run(rid="r1", status="ACTIVE", ticket="T1", role="dev", attempt=1):
    return SimpleNamespace(id=rid, status=status, ticket=ticket, role=role, attempt=attempt)


def test_runs():
    r = Rig()
    r.tr.tickets([root(), tk("T1")])
    r.tr.runs([run("old", "FAILED"), run("r1", "ACTIVE"), run("r2", "ACTIVE")])
    r.tr.runs([run("r1", "FAILED"), run("r2", "REPORTED")])
    r.tr.runs([run("r1", "ACTIVE")])                    # never traced twice
    dev = r.all("agent dev")
    assert len(dev) == 2
    failed = next(s for s in dev if attr(s, "legion.run.id") == "r1")
    assert failed.status.status_code == StatusCode.ERROR
    assert attr(failed, "legion.outcome") == "failed"
    t1 = r.tr._open["ticket:T1"].span.get_span_context().span_id
    assert all(s.parent.span_id == t1 for s in dev)


def test_end_abandoned_while_waiting_fails_the_waiting_ticket():
    r = Rig()
    r.tr.begin("w")
    r.tr.tickets([root(), tk("T1", "waiting", waiting="blocked"), tk("T2")])
    r.tr.end("abandoned")
    t1, t2, rt = r.one("agent:implementer T1"), r.one("agent:implementer T2"), r.one(f"change {CID}")
    assert (attr(t1, "legion.outcome"), t1.status.status_code) == ("failed", StatusCode.ERROR)
    assert (attr(t2, "legion.outcome"), t2.status.status_code) == ("abandoned", StatusCode.UNSET)
    assert (attr(rt, "legion.outcome"), rt.status.status_code) == ("abandoned", StatusCode.ERROR)
    assert not r.tr._open


def test_resume_marks_new_spans_and_skips_old_runs():
    r = Rig()
    r.tr.resume(["r1"])
    r.tr.tickets([root(), tk("T1"), tk("done", "passed")])
    r.tr.runs([run("r1", "ACTIVE"), run("r2", "ACTIVE")])
    r.tr.interrupt()
    assert attr(r.one(f"change {CID}"), "legion.resumed") is True
    assert attr(r.one("agent:implementer T1"), "legion.resumed") is True
    assert not r.all("agent:implementer done")
    assert [attr(s, "legion.run.id") for s in r.all("agent dev")] == ["r2"]


def test_after_the_end_runs_stay_in_the_trace():
    r = Rig()
    r.tr.begin("w")
    r.tr.end("completed")
    r.tr.runs([run("r9", "ACTIVE", ticket="change", role="retro")])
    r.tr.runs([run("r9", "REPORTED", ticket="change", role="retro")])
    retro = r.one("agent retro")
    assert retro.context.trace_id == tel.trace_id_for(CID)
    assert retro.parent.span_id == r.one(f"change {CID}").context.span_id


def test_interrupt_closes_everything():
    r = Rig()
    r.tr.begin("w")
    r.tr.tickets([root(), tk("T1")])
    r.tr.runs([run()])
    r.tr.interrupt()
    assert not r.tr._open
    assert len(r.spans) == 4
    assert all(s.status.status_code == StatusCode.UNSET for s in r.spans)
    assert attr(r.one(f"change {CID}"), "legion.outcome") == "interrupted"
    assert attr(r.one("agent dev"), "legion.outcome") == "interrupted"


# ---- allowlist, safety, shutdown -------------------------------------------------------------

def test_attrs_allowlist():
    out = tel._attrs(**{"legion.change.id": "x" * 300, "secret": "s", "legion.cycle": None,
                        "legion.resumed": False})
    assert out == {"legion.change.id": "x" * 128, "legion.resumed": False}


def test_guarded_never_raises(monkeypatch, caplog):
    r = Rig()

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(r.tr, "_make_root", boom)
    monkeypatch.setattr(r.tr, "_ticket", boom)
    monkeypatch.setattr(r.tr, "_close_all", boom)
    assert r.tr.begin("w") is None
    assert r.tr.tickets([root()]) is None
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
    tr.tickets([root()])
    tr.end("completed")
    t.shutdown()
    t.shutdown()
