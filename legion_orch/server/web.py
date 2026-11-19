"""HTTP surface: four listeners, one purpose each.

* ``server.host:port``          – operator: web UI + JSON API (human gates). Keep it off the
  sandbox network; nothing an agent needs is served here.
* ``server.agent_host:mcp_port``   – agents' MCP endpoint (``/mcp``), vetted by the gateway as
  ``protocol: mcp`` (an MCP endpoint may not share host:port with another protocol).
* ``server.agent_host:git_port``   – git smart-HTTP only (``/git/<repo>.git``).
* ``server.agent_host:token_port`` – ``/oauth/token`` only (SPIFFE JWT-SVID → access token), for
  the OpenShell gateway supervisor.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import signal
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from ..config import Config
from ..git import make_git_endpoint
from ..identity import make_token_endpoint
from ..sandbox import agentlog
from ..store import DocStoreError
from ..util import bearer
from ..workflow.engine import Controller, WorkflowError
from ..workflow.model import LIVE_RUN
from . import browse, views
from .browse import BrowseError
from .mcp import build_mcp

log = logging.getLogger("legion.server")
# The web UI: frontend/ built with bun (`bun run build`) into static/dist at packaging time.
UI_DIST = Path(__file__).parent / "static" / "dist"
NOT_BUILT = ("<!doctype html><title>Legion Orchestrator</title><p>The web UI is not built. Run "
             "<code>bun install &amp;&amp; bun run build</code> in <code>frontend/</code>.</p>")
# The UI renders agent-authored text. Scripts may only come from this origin (no inline script or
# inline event handlers), so injected markup cannot execute; responses are never sniffed or framed.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; "
                               "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
# Server-sent events: reconnect delay for the browser, the longest wait before re-checking, a
# pause after sending so a burst of transitions makes one update, and the idle keep-alive.
SSE_RETRY_MS, SSE_TICK_S, SSE_COALESCE_S, SSE_HEARTBEAT_S = 2000, 1.0, 0.1, 15.0
# The controller's own records (changes/<cid>/control/: state, graph, logs, verification) are
# hidden from the default document listing; they stay readable, and ?all=1 lists them.
CONTROL = "/control/"


def mcp_request_limit(cfg: Config) -> int:
    """The controller refuses MCP requests over this with a 413 the agent can read. It is half
    the gateway's inspection limit, so the gateway – which drops oversize requests without any
    response – never sees one the controller would not answer."""
    return cfg.server.mcp_max_body_bytes // 2


async def build_mcp_app(ctl: Controller) -> Starlette:
    path = urlparse(ctl.cfg.server.mcp_url).path or "/mcp"
    return (await build_mcp(ctl)).streamable_http_app(
        streamable_http_path=path, stateless_http=True, json_response=True,
        host=ctl.cfg.server.agent_host, max_request_body_size=mcp_request_limit(ctl.cfg))


def build_git_app(ctl: Controller) -> Starlette:
    endpoint = make_git_endpoint(
        ctl.git, lambda request: ctl.auth.git_grant(bearer(request.headers.get("authorization", ""))),
        max_request_bytes=ctl.cfg.limits.git_max_push_bytes,
        max_concurrency=ctl.cfg.limits.git_max_concurrency)
    return Starlette(routes=[Route("/git/{path:path}", endpoint, methods=["GET", "POST"])])


def build_token_app(ctl: Controller) -> Starlette:
    endpoint = make_token_endpoint(ctl.auth.identity, ctl.auth.sandbox_binding)
    return Starlette(routes=[Route("/oauth/token", endpoint, methods=["POST"])])


class _Server(uvicorn.Server):
    """A listener that leaves signals to :func:`serve`: uvicorn installs its own handlers per
    server, so with several in one process only the last one started would see Ctrl+C."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


GRACEFUL_SHUTDOWN_S = 5


async def build_servers(ctl: Controller, log_level: str = "info") -> list[uvicorn.Server]:
    """All listeners; run them together (see :func:`serve`). The operator app starts the controller."""
    s = ctl.cfg.server
    cfg = lambda app, host, port, **kw: _Server(uvicorn.Config(
        app, host=host, port=port, log_level=log_level,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S, **kw))
    # agent-facing: idle connections stay open while the model works (see agent_keep_alive_s)
    agent = {"timeout_keep_alive": s.agent_keep_alive_s}
    return [cfg(build_app(ctl), s.host, s.port),
            cfg(await build_mcp_app(ctl), s.agent_host, s.mcp_port, **agent),
            cfg(build_git_app(ctl), s.agent_host, s.git_port, **agent),
            cfg(build_token_app(ctl), s.agent_host, s.token_port, **agent)]


def request_shutdown(ctl: Controller, servers: list[uvicorn.Server]) -> None:
    """First Ctrl+C/SIGTERM: every listener shuts down gracefully (open UI streams end at once,
    other requests get GRACEFUL_SHUTDOWN_S). Again: stop without waiting."""
    if ctl.stopping.is_set():
        log.warning("forcing shutdown")
        for srv in servers:
            srv.force_exit = True
    else:
        log.info("shutting down (Ctrl+C again to force)")
    ctl.stopping.set()
    for srv in servers:
        srv.should_exit = True


async def serve(ctl: Controller) -> None:
    servers = await build_servers(ctl)
    loop = asyncio.get_running_loop()
    handled = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown, ctl, servers)
            handled.append(sig)
        except (NotImplementedError, RuntimeError):     # e.g. Windows, or not the main thread
            pass
    try:
        await asyncio.gather(*(srv.serve() for srv in servers))
    finally:
        for sig in handled:
            loop.remove_signal_handler(sig)


class BadRequest(Exception):
    """The request is malformed (400)."""


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise BadRequest("request body must be JSON") from None
    if not isinstance(body, dict):
        raise BadRequest("request body must be a JSON object")
    return body


def build_app(ctl: Controller) -> Starlette:
    """The operator app: UI and JSON API only. Reads are open to whoever can reach the listener;
    writes (human decisions) need the operator token when one is configured."""
    op_token = ctl.cfg.server.operator_token

    def check_operator(request: Request) -> None:
        if op_token and not secrets.compare_digest(request.headers.get("x-operator-token", ""), op_token):
            raise PermissionError("operator token required")

    def api(*, write: bool = False):
        """JSON endpoint: ``fn(request)`` (or ``fn(request, body)`` for writes, after the operator
        check) returns the response data; errors map to 400/403/404."""
        def deco(fn):
            async def endpoint(request: Request) -> Response:
                try:
                    if write:
                        check_operator(request)
                        data = await fn(request, await _json_object(request))
                    else:
                        data = await fn(request)
                except PermissionError as e:
                    return JSONResponse({"error": str(e)}, status_code=403)
                except (WorkflowError, BadRequest) as e:
                    return JSONResponse({"error": str(e)}, status_code=400)
                except (DocStoreError, BrowseError) as e:
                    return JSONResponse({"error": str(e)}, status_code=404)
                if isinstance(data, Response):
                    return data
                if isinstance(data, BaseModel):
                    data = data.model_dump(mode="json", exclude_none=True)
                return JSONResponse(data)
            return endpoint
        return deco

    def int_param(request: Request, name: str, default: int | None) -> int | None:
        raw = request.query_params.get(name)
        if raw is None:
            return default
        if not raw.isdigit():
            raise BadRequest(f"{name} must be a non-negative integer")
        return int(raw)

    # ---- static: the built UI (frontend/ -> static/dist)
    async def index(_: Request):
        page = UI_DIST / "index.html"
        if not page.is_file():
            return HTMLResponse(NOT_BUILT, status_code=503, headers=SECURITY_HEADERS)
        return FileResponse(page, headers=SECURITY_HEADERS | {"Cache-Control": "no-cache"})

    async def ui_file(request: Request):
        """Hashed bundle files (``/assets/…``) and the UI's public files (e.g. the icon)."""
        if request.method not in ("GET", "HEAD"):
            return PlainTextResponse("not found", status_code=404)
        rel = request.path_params["path"]
        root = UI_DIST.resolve()
        f = (root / rel).resolve()
        if not f.is_file() or root not in f.parents or f.name == "index.html":
            return PlainTextResponse("not found", status_code=404)
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
        return FileResponse(f, headers=SECURITY_HEADERS | {"Cache-Control": cache})

    async def workflow(_: Request):
        p = ctl.cfg.workflow_doc
        return PlainTextResponse(p.read_text() if p.is_file() else "(WORKFLOW.md not found)",
                                 media_type="text/markdown")

    # ---- reads
    @api()
    async def meta(_: Request):
        return {"project": ctl.cfg.project, "repo": ctl.cfg.repo.name,
                "decision_owners": ctl.cfg.decision_owners, "assets_sha": ctl.assets.sha,
                "version": ctl.version, "live_runs": ctl.live_count(),
                "max_parallel": ctl.cfg.limits.max_parallel}

    @api()
    async def list_changes(_: Request):
        return views.change_list(ctl)

    @api()
    async def attention(_: Request):
        return views.attention(ctl)

    @api()
    async def digest(request: Request):
        since = request.query_params.get("since", "")
        if not since:
            raise BadRequest("since (an ISO timestamp) is required")
        return views.digest(ctl, since)

    @api()
    async def state(request: Request):
        return views.change_state(ctl, ctl.get(request.path_params["cid"]))

    @api()
    async def docs(request: Request):
        items = ctl.docs.list(request.query_params.get("prefix", ""))
        if request.query_params.get("all") != "1":
            items = [d for d in items if CONTROL not in "/" + d.path]
        return [d.to_json() for d in items]

    @api()
    async def doc(request: Request):
        path = request.query_params.get("path")
        if not path:
            raise BadRequest("path is required")
        d = ctl.docs.read(path, int_param(request, "version", None))
        return d.to_json() | {"history": [m.to_json() for m in ctl.docs.history(path)]}

    @api()
    async def doc_diff(request: Request):
        path, old = request.query_params.get("path"), int_param(request, "from", None)
        if not path or old is None:
            raise BadRequest("path and from (a version) are required")
        return views.doc_diff(ctl.docs, path, old, int_param(request, "to", None))

    @api()
    async def trace(request: Request):
        return views.trace(ctl.get(request.path_params["cid"]))

    @api()
    async def acceptance(request: Request):
        return await views.acceptance(ctl.get(request.path_params["cid"]))

    # ---- browsing: workspaces, the repository, documents, configuration
    @api()
    async def list_workspaces(_: Request):
        return browse.workspaces(ctl)

    def workspace(request: Request) -> None:
        browse.check_workspace(ctl, request.path_params["ws"])

    @api()
    async def git_refs(request: Request):
        workspace(request)
        return await browse.git_refs(ctl)

    @api()
    async def git_tree(request: Request):
        workspace(request)
        return await browse.git_tree(ctl, request.query_params.get("ref", ""))

    @api()
    async def git_file(request: Request):
        workspace(request)
        q = request.query_params
        return await browse.git_file(ctl, q.get("ref", ""), q.get("path", ""),
                                     raw=q.get("download") == "1")

    @api()
    async def doc_raw(request: Request):
        path = request.query_params.get("path")
        if not path:
            raise BadRequest("path is required")
        return browse.doc_download(ctl, path, int_param(request, "version", None))

    @api()
    async def config_files(request: Request):
        return browse.config_files(ctl, request.path_params["group"])

    @api()
    async def config_file(request: Request):
        q = request.query_params
        return browse.config_file(ctl, request.path_params["group"], q.get("path", ""),
                                  raw=q.get("download") == "1")

    @api()
    async def run_log(request: Request):
        """The agent log: plain text, or ``?format=entries`` – the run's stage and failure with
        the log parsed into display entries (sandbox output and agent events)."""
        ce, run = ctl.find_run(request.path_params["run_id"])
        text = ce.log_tail(run.id, int_param(request, "lines", 300))
        if request.query_params.get("format") != "entries":
            return PlainTextResponse(text)
        return {"run": run.id, "status": run.status, "stage": run.stage,
                "failure": run.failure.model_dump(mode="json") if run.failure else None,
                "live": run.status in LIVE_RUN, "entries": agentlog.entries(text)}

    def sse(gen) -> StreamingResponse:
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def event(name: str, data) -> str:
        return f"event: {name}\ndata: {compact(data)}\n\n"

    def compact(data) -> str:
        return json.dumps(data, separators=(",", ":"))

    @api()
    async def change_events(request: Request):
        """Server-sent events for one change: ``state`` (everything the UI shows for it),
        ``changes`` (the change list) and ``attention`` (every change's attention items, §2.2) –
        each sent on connect and then whenever it differs, never polled. A comment line
        every ``SSE_HEARTBEAT_S`` keeps idle connections open."""
        return events(request, ctl.get(request.path_params["cid"]))

    @api()
    async def all_events(request: Request):
        """Server-sent events for the pages that show no single change: ``changes`` and
        ``attention``, as for a change's stream."""
        return events(request, None)

    def events(request: Request, ce) -> StreamingResponse:
        async def gen():
            yield f"retry: {SSE_RETRY_MS}\n\n"
            sent: dict[str, str] = {}
            seen, quiet = -1, 0.0
            while not ctl.stopping.is_set() and not await request.is_disconnected():
                if ctl.version != seen:
                    seen, quiet = ctl.version, 0.0
                    streams = [("changes", views.change_list(ctl)),
                               ("attention", views.attention(ctl))]
                    if ce is not None:
                        streams.append(("state", views.change_state(ctl, ce)))
                    for name, data in streams:
                        payload = compact(data)
                        if sent.get(name) != payload:
                            sent[name] = payload
                            yield f"event: {name}\ndata: {payload}\n\n"
                    await asyncio.sleep(SSE_COALESCE_S)     # a burst of transitions, one update
                elif quiet >= SSE_HEARTBEAT_S:
                    quiet = 0.0
                    yield ": keep-alive\n\n"
                await ctl.wait_version(seen, SSE_TICK_S)
                quiet += SSE_TICK_S
        return sse(gen())

    @api()
    async def log_events(request: Request):
        """Server-sent events for a run's agent log: ``log`` with the run's status and the display
        entries that changed (``from``: the first index that differs from what was sent; the
        client keeps entries before it), then ``end`` once the run has finished. The log grows
        without state changes, so it is re-read every ``SSE_TICK_S`` while the run is live."""
        ce, run = ctl.find_run(request.path_params["run_id"])
        lines = int_param(request, "lines", 5000)

        async def gen():
            yield f"retry: {SSE_RETRY_MS}\n\n"
            sent: list[dict] = []
            status = None
            while not ctl.stopping.is_set() and not await request.is_disconnected():
                entries = agentlog.entries(ce.log_tail(run.id, lines))
                head = {"run": run.id, "status": run.status, "stage": run.stage,
                        "failure": run.failure.model_dump(mode="json") if run.failure else None,
                        "live": run.status in LIVE_RUN}
                start = next((i for i, (a, b) in enumerate(zip(sent, entries, strict=False))
                              if a != b), min(len(sent), len(entries)))
                if start < len(entries) or len(entries) != len(sent) or head != status:
                    yield event("log", head | {"from": start, "entries": entries[start:],
                                               "total": len(entries)})
                    sent, status = entries, head
                if not head["live"]:
                    yield event("end", {"run": run.id})
                    return
                await ctl.wait_version(ctl.version, SSE_TICK_S)
        return sse(gen())

    # ---- writes (human decisions)
    @api(write=True)
    async def start_change(_: Request, body: dict):
        ce = await ctl.start_change(str(body.get("goal", "")))
        return {"change": ce.cid}

    @api(write=True)
    async def node_action(request: Request, body: dict):
        ce = ctl.get(request.path_params["cid"])
        return await ce.human_action(request.path_params["node_id"], body.get("action", ""),
                                     body.get("approver", ""), body.get("payload") or {})

    @api(write=True)
    async def change_action(request: Request, body: dict):
        ce = ctl.get(request.path_params["cid"])
        return await ce.change_action(body.get("action", ""), body.get("approver", ""),
                                      body.get("payload") or {})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await ctl.start()
        try:
            yield
        finally:
            await ctl.stop()

    return Starlette(routes=[
        Route("/", index),
        Route("/api/workflow", workflow),
        Route("/api/meta", meta),
        Route("/api/changes", list_changes, methods=["GET"]),
        Route("/api/attention", attention),
        Route("/api/digest", digest),
        Route("/api/changes", start_change, methods=["POST"]),
        Route("/api/changes/{cid}/state", state),
        Route("/api/changes/{cid}/nodes/{node_id}/action", node_action, methods=["POST"]),
        Route("/api/changes/{cid}/action", change_action, methods=["POST"]),
        Route("/api/changes/{cid}/events", change_events),
        Route("/api/events", all_events),
        Route("/api/workspaces", list_workspaces),
        Route("/api/workspaces/{ws}/git/refs", git_refs),
        Route("/api/workspaces/{ws}/git/tree", git_tree),
        Route("/api/workspaces/{ws}/git/file", git_file),
        Route("/api/config/{group}", config_files),
        Route("/api/config/{group}/file", config_file),
        Route("/api/changes/{cid}/trace", trace),
        Route("/api/changes/{cid}/acceptance", acceptance),
        Route("/api/runs/{run_id}/log/events", log_events),
        Route("/api/docs", docs),
        Route("/api/doc", doc),
        Route("/api/doc/diff", doc_diff),
        Route("/api/doc/raw", doc_raw),
        Route("/api/runs/{run_id}/log", run_log),
        # last: the UI's bundle and public files (anything else, by any method, is a 404)
        Route("/{path:path}", ui_file, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]),
    ], lifespan=lifespan)
