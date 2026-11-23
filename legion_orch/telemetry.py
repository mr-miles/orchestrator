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
    "legion.change.id", "legion.workspace", "legion.ticket.id", "legion.ticket.type",
    "legion.attempt", "legion.status", "legion.waiting", "legion.outcome", "legion.run.id",
    "legion.run.role", "legion.resumed",
})

_MAX_STR = 128
_LIVE_RUN = ("STARTING", "ACTIVE")
_CLOSED = ("passed", "failed", "cancelled")   # closed: nothing more happens to it
_WAITING = "waiting"


def trace_id_for(cid: str) -> int:
    digest = hashlib.sha256(("legion-change:" + cid).encode()).digest()
    return int.from_bytes(digest[:16], "big") or 1


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

    def tickets(self, tickets: Iterable[Any]) -> None:
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
    __slots__ = ("span", "name")

    def __init__(self, span: Any, name: str = "") -> None:
        self.span = span
        self.name = name


class ChangeTrace(NullTrace):
    """The spans of one change: one trace (its id derived from the change's), the change at its
    root; a span per ticket – from leaving ``pending`` to a closed status (passed, failed,
    cancelled) – under its parent ticket's span, its status changes on the way as events (with
    the attempt: a rework, a criterion's next round); a span per agent run under its
    ticket's. Fed from the change's saves (:meth:`tickets`, :meth:`runs`)."""

    def __init__(self, tracer: Any, ids: Any, cid: str) -> None:
        self._tracer = tracer
        self._ids = ids
        self._cid = cid
        self._tid = trace_id_for(cid)
        self._root: Any = None
        self._root_open = False
        self._open: dict[str, _Sp] = {}
        self._seen: dict[str, tuple[str, int, str | None]] = {}   # status, attempt, waiting
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

    def _add(self, key: str, name: str, attrs: dict[str, Any], parent: Any) -> None:
        span = self._start(name, attrs, parent if parent is not None else self._anchor())
        self._open[key] = _Sp(span, name)

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

    def _span(self, key: str | None) -> Any:
        sp = self._open.get(key) if key else None
        return sp.span if sp else None

    # ---- hooks ----------------------------------------------------------------------------

    @_guarded
    def begin(self, workspace: str) -> None:
        if self._root is None:
            self._make_root(workspace)

    @_guarded
    def resume(self, run_ids: Iterable[str], ended: bool = False) -> None:
        """After a restart: the runs already traced (or over) are not traced again; tickets
        under way get a new span (``legion.resumed``), closed ones none."""
        self._resumed = True
        self._ended = ended
        self._runs_skipped |= set(run_ids)

    @_guarded
    def tickets(self, tickets: Iterable[Any]) -> None:
        by_id = {str(t.id): t for t in tickets}
        for tid, t in by_id.items():
            self._ticket(tid, t, by_id)

    def _ticket(self, tid: str, t: Any, by_id: Mapping[str, Any]) -> None:
        status, attempt = _text(t.status), int(getattr(t, "attempt", 0) or 0)
        waiting = _text(t.waiting) if getattr(t, "waiting", None) else None
        before = self._seen.get(tid)
        self._seen[tid] = (status, attempt, waiting)
        key = "ticket:" + tid
        if before == (status, attempt, waiting):
            return
        if key not in self._open:
            if status == "pending" or (status in _CLOSED and (
                    before is None or (status != "passed" and before[0] == "pending"))):
                return      # not started; closed before we knew of it; or dropped unstarted
            parent = by_id.get(str(t.parent)) if t.parent else None
            if parent is not None and "ticket:" + str(parent.id) not in self._open:
                self._ticket(str(parent.id), parent, by_id)
            self._add(key, f"{_text(t.type)} {tid}", {
                "legion.change.id": self._cid, "legion.ticket.id": tid,
                "legion.ticket.type": _text(t.type), "legion.attempt": attempt,
                "legion.resumed": True if before is None and self._resumed else None,
            }, self._span("ticket:" + str(t.parent)) if t.parent else None)
        if status in _CLOSED:
            self._close(key, {"legion.outcome": status, "legion.attempt": attempt})
            return
        self._open[key].span.add_event(status, _attrs(**{
            "legion.status": status, "legion.attempt": attempt, "legion.waiting": waiting}))

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
                    self._add(key, f"agent {r.role}", {
                        "legion.change.id": self._cid, "legion.run.id": rid,
                        "legion.run.role": r.role, "legion.ticket.id": r.ticket,
                        "legion.attempt": r.attempt}, self._span("ticket:" + str(r.ticket)))
                else:
                    self._runs_skipped.add(rid)

    def _close_all(self, status: str | None) -> None:
        """Close what is open, children first. status None: everything ``interrupted``."""
        for k in [k for k in reversed(list(self._open)) if k.startswith("run:")]:
            self._close(k, {"legion.outcome": "interrupted"})
        for k in [k for k in reversed(list(self._open)) if k.startswith("ticket:")]:
            waited = self._seen.get(k[len("ticket:"):], ("", 0, None))[0] == _WAITING
            if status is not None and status != "completed" and waited:
                self._close(k, {"legion.outcome": "failed"}, status)
            else:
                self._close(k, {"legion.outcome": "abandoned" if status else "interrupted"})

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
        """The change ended (``completed`` or ``abandoned``): what is still open closes. Runs
        after it (the retrospective) are parentless spans in the same trace."""
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
