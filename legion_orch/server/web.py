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
import secrets
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from ..git import make_git_endpoint
from ..identity import make_token_endpoint
from ..store import DocStoreError
from ..util import bearer
from ..workflow.engine import Controller, WorkflowError
from . import views
from .mcp import build_mcp

STATIC = Path(__file__).parent / "static"
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
# Bulky controller records hidden from the default document listing (still readable).
NOISY = ("/control/nodes/", "/control/runs/")


def build_mcp_app(ctl: Controller) -> Starlette:
    path = urlparse(ctl.cfg.server.mcp_url).path or "/mcp"
    return build_mcp(ctl).streamable_http_app(
        streamable_http_path=path, stateless_http=True, json_response=True,
        host=ctl.cfg.server.agent_host)


def build_git_app(ctl: Controller) -> Starlette:
    endpoint = make_git_endpoint(
        ctl.git, lambda request: ctl.auth.git_grant(bearer(request.headers.get("authorization", ""))),
        max_request_bytes=ctl.cfg.limits.git_max_push_bytes,
        max_concurrency=ctl.cfg.limits.git_max_concurrency)
    return Starlette(routes=[Route("/git/{path:path}", endpoint, methods=["GET", "POST"])])


def build_token_app(ctl: Controller) -> Starlette:
    endpoint = make_token_endpoint(ctl.auth.identity, ctl.auth.sandbox_binding)
    return Starlette(routes=[Route("/oauth/token", endpoint, methods=["POST"])])


def build_servers(ctl: Controller, log_level: str = "info") -> list[uvicorn.Server]:
    """All listeners; run them together (see :func:`serve`). The operator app starts the controller."""
    s = ctl.cfg.server
    cfg = lambda app, host, port: uvicorn.Server(uvicorn.Config(app, host=host, port=port,
                                                                log_level=log_level))
    return [cfg(build_app(ctl), s.host, s.port),
            cfg(build_mcp_app(ctl), s.agent_host, s.mcp_port),
            cfg(build_git_app(ctl), s.agent_host, s.git_port),
            cfg(build_token_app(ctl), s.agent_host, s.token_port)]


async def serve(ctl: Controller) -> None:
    await asyncio.gather(*(srv.serve() for srv in build_servers(ctl)))


def build_app(ctl: Controller) -> Starlette:
    """The operator app: UI and JSON API only."""
    op_token = ctl.cfg.server.operator_token

    def operator(request: Request) -> None:
        if op_token and not secrets.compare_digest(request.headers.get("x-operator-token", ""), op_token):
            raise PermissionError("operator token required")

    def guarded(fn):
        async def wrapper(request: Request):
            try:
                return await fn(request)
            except PermissionError as e:
                return JSONResponse({"error": str(e)}, status_code=403)
            except WorkflowError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        return wrapper

    async def index(_: Request):
        return FileResponse(STATIC / "index.html", headers=SECURITY_HEADERS)

    async def app_js(_: Request):
        return FileResponse(STATIC / "app.js", media_type="text/javascript", headers=SECURITY_HEADERS)

    async def meta(_: Request):
        return JSONResponse({"project": ctl.cfg.project, "repo": ctl.cfg.repo.name,
                             "decision_owners": ctl.cfg.decision_owners,
                             "assets_sha": ctl.assets.sha, "version": ctl.version,
                             "live_runs": ctl.live_count(),
                             "max_parallel": ctl.cfg.limits.max_parallel})

    @guarded
    async def changes(request: Request):
        if request.method == "POST":
            operator(request)
            body = await request.json()
            ce = await ctl.start_change(body.get("goal", ""))
            return JSONResponse({"ok": True, "change": ce.cid})
        return JSONResponse(views.change_list(ctl))

    @guarded
    async def state(request: Request):
        return JSONResponse(views.change_state(ctl, ctl.get(request.path_params["cid"])))

    @guarded
    async def action(request: Request):
        operator(request)
        body = await request.json()
        ce = ctl.get(request.path_params["cid"])
        return JSONResponse(await ce.human_action(
            request.path_params["node_id"], body.get("action", ""), body.get("approver", ""),
            body.get("payload") or {}))

    @guarded
    async def change_action(request: Request):
        operator(request)
        body = await request.json()
        ce = ctl.get(request.path_params["cid"])
        return JSONResponse(await ce.change_action(body.get("action", ""), body.get("approver", ""),
                                                   body.get("payload") or {}))

    async def stream(request: Request):
        async def gen():
            last = -1
            while not await request.is_disconnected():
                if ctl.version != last:
                    last = ctl.version
                    yield f"data: {json.dumps({'version': last})}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    async def docs(request: Request):
        prefix = request.query_params.get("prefix", "")
        items = ctl.docs.list(prefix)
        if request.query_params.get("all") != "1":
            items = [d for d in items if not any(n in "/" + d["path"] for n in NOISY)]
        return JSONResponse(items)

    async def doc(request: Request):
        path = request.query_params["path"]
        ver = request.query_params.get("version")
        try:
            d = ctl.docs.read(path, int(ver) if ver else None)
        except DocStoreError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse(d.meta() | {"content": d.content, "history": ctl.docs.history(path)})

    @guarded
    async def run_log(request: Request):
        ce, run = ctl.find_run(request.path_params["run_id"])
        return PlainTextResponse(ce.log_tail(run.id, int(request.query_params.get("lines", 300))))

    async def workflow(_: Request):
        p = ctl.cfg.workflow_doc
        return PlainTextResponse(p.read_text() if p.is_file() else "(WORKFLOW.md not found)",
                                 media_type="text/markdown")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await ctl.start()
        try:
            yield
        finally:
            await ctl.stop()

    return Starlette(routes=[
        Route("/", index),
        Route("/app.js", app_js),
        Route("/api/meta", meta),
        Route("/api/changes", changes, methods=["GET", "POST"]),
        Route("/api/changes/{cid}/state", state),
        Route("/api/changes/{cid}/nodes/{node_id}/action", action, methods=["POST"]),
        Route("/api/changes/{cid}/action", change_action, methods=["POST"]),
        Route("/api/stream", stream),
        Route("/api/docs", docs),
        Route("/api/doc", doc),
        Route("/api/runs/{run_id}/log", run_log),
        Route("/api/workflow", workflow),
    ], lifespan=lifespan)
