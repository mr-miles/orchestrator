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
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from ..git import make_git_endpoint
from ..identity import make_token_endpoint
from ..sandbox import agentlog
from ..store import DocStoreError
from ..util import bearer
from ..workflow.engine import Controller, WorkflowError
from ..workflow.model import LIVE_RUN
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


async def build_mcp_app(ctl: Controller) -> Starlette:
    path = urlparse(ctl.cfg.server.mcp_url).path or "/mcp"
    return (await build_mcp(ctl)).streamable_http_app(
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


async def build_servers(ctl: Controller, log_level: str = "info") -> list[uvicorn.Server]:
    """All listeners; run them together (see :func:`serve`). The operator app starts the controller."""
    s = ctl.cfg.server
    cfg = lambda app, host, port: uvicorn.Server(uvicorn.Config(app, host=host, port=port,
                                                                log_level=log_level))
    return [cfg(build_app(ctl), s.host, s.port),
            cfg(await build_mcp_app(ctl), s.agent_host, s.mcp_port),
            cfg(build_git_app(ctl), s.agent_host, s.git_port),
            cfg(build_token_app(ctl), s.agent_host, s.token_port)]


async def serve(ctl: Controller) -> None:
    await asyncio.gather(*(srv.serve() for srv in await build_servers(ctl)))


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
                except DocStoreError as e:
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

    # ---- static
    async def index(_: Request):
        return FileResponse(STATIC / "index.html", headers=SECURITY_HEADERS)

    async def app_js(_: Request):
        return FileResponse(STATIC / "app.js", media_type="text/javascript", headers=SECURITY_HEADERS)

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
    async def state(request: Request):
        return views.change_state(ctl, ctl.get(request.path_params["cid"]))

    @api()
    async def docs(request: Request):
        items = ctl.docs.list(request.query_params.get("prefix", ""))
        if request.query_params.get("all") != "1":
            items = [d for d in items if not any(n in "/" + d.path for n in NOISY)]
        return [d.to_json() for d in items]

    @api()
    async def doc(request: Request):
        path = request.query_params.get("path")
        if not path:
            raise BadRequest("path is required")
        d = ctl.docs.read(path, int_param(request, "version", None))
        return d.to_json() | {"history": [m.to_json() for m in ctl.docs.history(path)]}

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
        Route("/app.js", app_js),
        Route("/api/workflow", workflow),
        Route("/api/meta", meta),
        Route("/api/changes", list_changes, methods=["GET"]),
        Route("/api/changes", start_change, methods=["POST"]),
        Route("/api/changes/{cid}/state", state),
        Route("/api/changes/{cid}/nodes/{node_id}/action", node_action, methods=["POST"]),
        Route("/api/changes/{cid}/action", change_action, methods=["POST"]),
        Route("/api/stream", stream),
        Route("/api/docs", docs),
        Route("/api/doc", doc),
        Route("/api/runs/{run_id}/log", run_log),
    ], lifespan=lifespan)
