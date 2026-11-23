"""Optional OpenTelemetry tracing of the workflow: one trace per change.

This module imports no engine module and no ``opentelemetry`` package at import time: every
OpenTelemetry import is inside a function, so the controller runs with the packages absent.
Every public method of :class:`ChangeTrace` is guarded: tracing never raises into the engine.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

INSTALL_HINT = ("install the OpenTelemetry packages: uv sync --extra telemetry "
                "(pip install 'legion-orchestrator[telemetry]')")


class TelemetryUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("telemetry is enabled but the OpenTelemetry SDK/OTLP exporter is not "
                         "installed: " + INSTALL_HINT)


ATTRS = frozenset({
    "legion.change.id", "legion.workspace", "legion.stage.id", "legion.stage.title",
    "legion.group.id", "legion.node.id", "legion.node.kind", "legion.cycle", "legion.phase",
    "legion.result", "legion.outcome", "legion.run.id", "legion.run.role",
    "legion.escalation.kind", "legion.expected", "legion.resumed",
})

_MAX_STR = 128
_LIVE_RUN = ("STARTING", "ACTIVE")
_WAITING = ("ESCALATED", "AWAITING_HUMAN")


def trace_id_for(cid: str) -> int:
    digest = hashlib.sha256(("legion-change:" + cid).encode()).digest()
    return int.from_bytes(digest[:16], "big") or 1


@dataclass(frozen=True)
class Subject:
    """What a flow node is (built by the flow runtime)."""
    level: str                 # "stage" | "group" | "step"
    parent: str | None         # enclosing stage/group id, None at top level
    kind: str | None           # steps: node kind value ("" if unknown); else None
    title: str | None          # stages: the static title; else None


@dataclass(frozen=True)
class NodeMove:
    """One change of a node's status (built by the step runner)."""
    node_id: str
    kind: str
    cycle: int
    before: str | None         # None = adopted after a restart
    to: str
    phase: str | None          # class name of the phase for `to`, None if in no phase
    order: int | None          # index of that phase in the step type's phases
    escalation_kind: str | None
    expected: bool


def _attrs(**kw: Any) -> dict[str, Any]:
    """The attribute allowlist: unknown keys and None dropped, strings truncated."""
    out: dict[str, Any] = {}
    for k, v in kw.items():
        if k not in ATTRS or v is None:
            continue
        out[k] = v[:_MAX_STR] if isinstance(v, str) else v
    return out


def _text(v: Any) -> str:
    return str(getattr(v, "value", v))


class NullTrace:
    """The disabled case: every method is a no-op."""

    def begin(self, workspace: str) -> None:
        return None

    def resume(self, run_ids: Iterable[str], ended: bool = False) -> None:
        return None

    def flow(self, entries: Iterable[tuple[str, str, str, str]],
             describe: Callable[[str], Subject | None], ended: bool = False) -> None:
        return None

    def node(self, move: NodeMove) -> None:
        return None

    def runs(self, runs: Iterable[Any]) -> None:
        return None

    def end(self, status: str) -> None:
        return None

    def interrupt(self) -> None:
        return None


_warned: set[str] = set()


def _guarded(fn: Callable[..., Any]) -> Callable[..., None]:
    @wraps(fn)
    def wrapper(*a: Any, **kw: Any) -> None:
        try:
            fn(*a, **kw)
        except Exception as exc:  # tracing must never break the engine
            if fn.__name__ not in _warned:
                _warned.add(fn.__name__)
                log.warning("telemetry: %s failed (%s: %s); further failures of it are not "
                            "logged", fn.__name__, type(exc).__name__, exc)
        return None
    return wrapper


class _Sp:
    __slots__ = ("span", "name", "order")

    def __init__(self, span: Any, name: str = "", order: int | None = None) -> None:
        self.span = span
        self.name = name
        self.order = order


class ChangeTrace(NullTrace):
    """The spans of one change (see the module's spec: one trace, deterministic id)."""

    def __init__(self, tracer: Any, ids: Any, cid: str) -> None:
        self._tracer = tracer
        self._ids = ids
        self._cid = cid
        self._tid = trace_id_for(cid)
        self._root: Any = None
        self._root_open = False
        self._open: dict[str, _Sp] = {}
        self._waiting: set[str] = set()
        self._cycles: dict[str, int] = {}
        self._runs_skipped: set[str] = set()
        self._resumed = False
        self._ended = False

    # ---- span plumbing ------------------------------------------------------------------

    def _start(self, name: str, attrs: dict[str, Any], parent: Any) -> Any:
        from opentelemetry.context import Context
        from opentelemetry.trace import set_span_in_context
        if parent is None:
            attrs = dict(attrs)
            if self._resumed:
                attrs["legion.resumed"] = True
            ctx = Context()
            self._ids.forced = self._tid
        else:
            ctx = set_span_in_context(parent)
        try:
            return self._tracer.start_span(name, context=ctx, attributes=_attrs(**attrs))
        finally:
            self._ids.forced = None

    def _make_root(self, workspace: str | None = None) -> None:
        attrs: dict[str, Any] = {"legion.change.id": self._cid, "legion.workspace": workspace}
        self._root = self._start(f"change {self._cid}", attrs, None)
        self._root_open = True

    def _anchor(self) -> Any:
        """Parent for a span whose natural parent is missing (None: start it parentless)."""
        if self._root is None and not self._ended:
            self._make_root()
        return self._root

    def _find(self, nid: str | None) -> Any:
        if nid is None:
            return None
        sp = self._open.get("stage:" + nid) or self._open.get("group:" + nid)
        return sp.span if sp else None

    def _add(self, key: str, name: str, attrs: dict[str, Any], parent: Any,
             order: int | None = None) -> None:
        span = self._start(name, attrs, parent if parent is not None else self._anchor())
        self._open[key] = _Sp(span, name, order)

    def _close(self, key: str, attrs: dict[str, Any], error: str | None = None) -> bool:
        from opentelemetry.trace import Status, StatusCode
        sp = self._open.pop(key, None)
        if sp is None:
            return False
        if attrs:
            sp.span.set_attributes(_attrs(**attrs))
        if error is not None:
            sp.span.set_status(Status(StatusCode.ERROR, error[:_MAX_STR]))
        sp.span.end()
        return True

    def _close_phase(self, nid: str, result: str, error: str | None = None) -> None:
        self._close("phase:" + nid, {"legion.result": result}, error)

    def _close_step(self, nid: str, outcome: str, error: str | None = None) -> None:
        self._close_phase(nid, "ok" if outcome == "accepted" else "sent_back")
        self._waiting.discard(nid)
        attrs: dict[str, Any] = {"legion.outcome": outcome, "legion.cycle": self._cycles.get(nid)}
        self._close("step:" + nid, attrs, error)

    def _event(self, nid: str, name: str, attrs: dict[str, Any] | None = None) -> None:
        sp = self._open.get("step:" + nid) or self._open.get("phase:" + nid)
        if sp is not None:
            sp.span.add_event(name, _attrs(**(attrs or {})))

    # ---- hooks ----------------------------------------------------------------------------

    @_guarded
    def begin(self, workspace: str) -> None:
        if self._root is None:
            self._make_root(workspace)

    @_guarded
    def resume(self, run_ids: Iterable[str], ended: bool = False) -> None:
        self._resumed = True
        self._ended = ended
        self._runs_skipped |= set(run_ids)

    @_guarded
    def flow(self, entries: Iterable[tuple[str, str, str, str]],
             describe: Callable[[str], Subject | None], ended: bool = False) -> None:
        for nid, transition, _before, _to in entries:
            self._flow_one(nid, transition, describe(nid), ended)

    def _flow_one(self, nid: str, transition: str, subj: Subject | None, ended: bool) -> None:
        if subj is None:
            if transition in ("complete", "override"):
                outcome = "accepted"
            elif transition == "removed":
                outcome = "abandoned"
            elif transition == "reset" and not ended:
                outcome = "rework"
            else:
                return
            for level in ("stage", "group"):
                self._close(f"{level}:{nid}", {"legion.outcome": outcome})
            self._close_step(nid, outcome)
            return
        level = subj.level
        key = f"{level}:{nid}"
        resumed = True if transition == "adopt" else None
        parent = self._find(subj.parent)
        base: dict[str, Any] = {"legion.change.id": self._cid, "legion.resumed": resumed}
        if level == "step":
            if transition in ("start", "rerun", "adopt"):
                if key in self._open:
                    self._close_step(nid, "rework")
                self._add(key, f"step {nid}", {
                    **base, "legion.node.id": nid, "legion.node.kind": subj.kind}, parent)
            elif transition in ("complete", "override"):
                self._close_step(nid, "accepted")
            elif transition == "reset":
                if not ended:
                    self._close_step(nid, "rework")
            elif transition == "removed":
                self._close_step(nid, "abandoned")
            return
        if transition in ("start", "reopen", "adopt"):
            if key not in self._open:
                ids = ({"legion.stage.id": nid, "legion.stage.title": subj.title}
                       if level == "stage" else {"legion.group.id": nid})
                self._add(key, f"{level} {nid if level == 'group' else subj.title or nid}",
                          {**base, **ids}, parent)
        elif transition == "complete":
            self._close(key, {"legion.outcome": "accepted"})
        elif transition == "reset":
            self._close(key, {"legion.outcome": "rework"})
        elif transition == "removed":
            self._close(key, {"legion.outcome": "abandoned"})

    @_guarded
    def node(self, move: NodeMove) -> None:
        nid, to = move.node_id, move.to
        self._cycles[nid] = move.cycle
        if to == "PENDING":
            self._close_phase(nid, "sent_back")
            self._waiting.discard(nid)
        elif to == "ACCEPTED":
            self._close_phase(nid, "ok")
        elif to == "FAILED":
            self._close_phase(nid, "failed", "failed")
        elif to in _WAITING:
            if move.before is not None:
                kind = move.escalation_kind or "escalated"
                self._close_phase(nid, "escalate", None if move.expected else kind)
            self._event(nid, "awaiting_human" if to == "AWAITING_HUMAN" else "escalated", {
                "legion.escalation.kind": move.escalation_kind,
                "legion.expected": move.expected})
            self._waiting.add(nid)
        elif move.phase is not None:
            self._phase(move)

    def _phase(self, move: NodeMove) -> None:
        nid = move.node_id
        if nid in self._waiting:
            self._event(nid, "resumed")
            self._waiting.discard(nid)
        cur = self._open.get("phase:" + nid)
        if cur is not None and cur.name == f"phase {move.phase}":
            self._event(nid, f"status {move.to}")
            return
        if cur is not None:
            ok = move.order is not None and cur.order is not None and move.order > cur.order
            self._close_phase(nid, "ok" if ok else "rework")
        step = self._open.get("step:" + nid)
        self._add("phase:" + nid, f"phase {move.phase}", {
            "legion.change.id": self._cid, "legion.node.id": nid, "legion.node.kind": move.kind,
            "legion.cycle": move.cycle, "legion.phase": move.phase,
            "legion.resumed": True if move.before is None else None,
        }, step.span if step else None, move.order)

    @_guarded
    def runs(self, runs: Iterable[Any]) -> None:
        for r in runs:
            rid, key = str(r.id), "run:" + str(r.id)
            status = _text(r.status)
            live = status in _LIVE_RUN
            if key in self._open:
                if not live:
                    self._close(key, {"legion.outcome": status.lower()},
                                "failed" if status == "FAILED" else None)
                    self._runs_skipped.add(rid)
            elif rid not in self._runs_skipped:
                if live:
                    step = self._open.get("step:" + str(r.ticket))
                    self._add(key, f"agent {r.role}", {
                        "legion.change.id": self._cid, "legion.run.id": rid,
                        "legion.run.role": r.role, "legion.node.id": r.ticket,
                        "legion.cycle": r.attempt}, step.span if step else None)
                else:
                    self._runs_skipped.add(rid)

    def _close_all(self, status: str | None) -> None:
        """Close what is open, children first. status None: everything `interrupted`."""
        def keys(prefix: str) -> list[str]:
            return [k for k in reversed(list(self._open)) if k.startswith(prefix)]
        for k in keys("run:"):
            self._close(k, {"legion.outcome": "interrupted"})
        for k in keys("phase:"):
            self._close(k, {"legion.result": "interrupted"})
        for k in keys("step:"):
            nid = k[len("step:"):]
            if status is not None and status != "completed" and nid in self._waiting:
                self._close(k, {"legion.outcome": "failed",
                                "legion.cycle": self._cycles.get(nid)}, status)
            else:
                self._close(k, {"legion.outcome": "abandoned" if status else "interrupted",
                                "legion.cycle": self._cycles.get(nid)})
        for prefix in ("group:", "stage:"):
            for k in keys(prefix):
                self._close(k, {"legion.outcome": "abandoned" if status else "interrupted"})
        self._waiting.clear()

    def _close_root(self, outcome: str, error: bool) -> None:
        from opentelemetry.trace import Status, StatusCode
        if self._root is None or not self._root_open:
            return
        self._root_open = False
        self._root.set_attributes(_attrs(**{"legion.outcome": outcome}))
        if error:
            self._root.set_status(Status(StatusCode.ERROR, outcome))
        self._root.end()

    @_guarded
    def end(self, status: str) -> None:
        if self._root is None and not self._ended:
            self._make_root()
        self._ended = True
        self._close_all(status)
        self._close_root(status, status != "completed")

    @_guarded
    def interrupt(self) -> None:
        self._ended = True
        self._close_all(None)
        self._close_root("interrupted", False)


def _make_ids() -> Any:
    from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

    class _Ids(RandomIdGenerator):
        """Trace-id generator that yields the forced id once, when one is set."""

        def __init__(self) -> None:
            self.forced: int | None = None

        def generate_trace_id(self) -> int:
            forced = self.forced
            if forced is not None:
                self.forced = None
                return forced
            return super().generate_trace_id()

    return _Ids()


def _provider(exporter: Any, project: str, service_name: str, batch: bool) -> tuple[Any, Any]:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    ids = _make_ids()
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name, "legion.project": project}),
        id_generator=ids)
    if batch:
        provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=2000))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, ids


class Telemetry:
    """Process-wide tracing handle: builds a ChangeTrace per change, flushes at shutdown."""

    def __init__(self, provider: Any = None, ids: Any = None) -> None:
        self._provider = provider
        self._ids = ids
        self._tracer = provider.get_tracer("legion_orch") if provider is not None else None
        self.enabled = provider is not None
        self._traces: dict[str, ChangeTrace] = {}
        self._down = False

    @classmethod
    def off(cls) -> Telemetry:
        return cls()

    @classmethod
    def from_config(cls, cfg: Config, env: Mapping[str, str] | None = None) -> Telemetry:
        env = os.environ if env is None else env
        tc = cfg.telemetry
        if tc.enabled is False:
            return cls.off()
        if tc.enabled is None:
            endpoint = (env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
                        or env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))
            if (not endpoint or env.get("OTEL_SDK_DISABLED", "").lower() == "true"
                    or env.get("OTEL_TRACES_EXPORTER") == "none"):
                return cls.off()
        try:
            import opentelemetry.exporter.otlp.proto.http.trace_exporter as otlp
            import opentelemetry.sdk.resources  # noqa: F401
            import opentelemetry.sdk.trace  # noqa: F401
        except ImportError:
            if tc.enabled is True:
                raise TelemetryUnavailable() from None
            log.warning("%s", TelemetryUnavailable())
            return cls.off()
        kwargs: dict[str, Any] = {"timeout": tc.timeout_s}
        if tc.endpoint:
            url = tc.endpoint.rstrip("/")
            kwargs["endpoint"] = url if url.endswith("/v1/traces") else url + "/v1/traces"
        exporter = otlp.OTLPSpanExporter(**kwargs)
        name = tc.service_name or env.get("OTEL_SERVICE_NAME") or "legion-orchestrator"
        provider, ids = _provider(exporter, cfg.project, name, True)
        return cls(provider, ids)

    @classmethod
    def for_exporter(cls, exporter: Any, *, project: str = "test",
                     service_name: str = "legion-orchestrator", batch: bool = False) -> Telemetry:
        provider, ids = _provider(exporter, project, service_name, batch)
        return cls(provider, ids)

    def change(self, cid: str) -> NullTrace:
        if not self.enabled:
            return NullTrace()
        try:
            trace = ChangeTrace(self._tracer, self._ids, cid)
        except Exception as exc:
            log.warning("telemetry: cannot trace change (%s)", type(exc).__name__)
            return NullTrace()
        self._traces[cid] = trace
        return trace

    def shutdown(self, timeout_s: float = 5.0) -> None:
        if not self.enabled or self._down:
            return
        self._down = True
        for trace in list(self._traces.values()):
            try:
                trace.interrupt()
            except Exception as exc:
                log.warning("telemetry: interrupt failed (%s)", type(exc).__name__)
        try:
            self._provider.force_flush(int(timeout_s * 1000))
        except Exception as exc:
            log.warning("telemetry: flush failed (%s)", type(exc).__name__)
        try:
            self._provider.shutdown()
        except Exception as exc:
            log.warning("telemetry: shutdown failed (%s)", type(exc).__name__)
