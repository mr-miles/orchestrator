"""HTTP surface, two listeners:

* ``server.port``     – web UI + JSON API, git smart-HTTP (``/git/``), token endpoint (``/oauth/token``)
* ``server.mcp_port`` – the agents' MCP endpoint (``/mcp``) on its own port, so the OpenShell
  gateway can vet it as ``protocol: mcp`` (an MCP endpoint may not share host:port with REST).
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

from ..engine import Controller, WorkflowError
from ..git import make_git_endpoint
from ..identity import make_token_endpoint
from ..store import DocStoreError
from ..util import bearer
from .mcp import build_mcp

STATIC = Path(__file__).parent / "static"
# Bulky controller records hidden from the default document listing (still readable).
NOISY = ("/control/events/", "/control/nodes/", "/control/runs/")


def build_mcp_app(ctl: Controller) -> Starlette:
    path = urlparse(ctl.cfg.server.mcp_url).path or "/mcp"
    return build_mcp(ctl).streamable_http_app(
        streamable_http_path=path, stateless_http=True, json_response=True, host=ctl.cfg.server.host)


def build_servers(ctl: Controller, log_level: str = "info") -> list[uvicorn.Server]:
    """Both listeners; run them together (see :func:`serve`)."""
    s = ctl.cfg.server
    return [uvicorn.Server(uvicorn.Config(build_app(ctl), host=s.host, port=s.port, log_level=log_level)),
            uvicorn.Server(uvicorn.Config(build_mcp_app(ctl), host=s.host, port=s.mcp_port,
                                          log_level=log_level))]


async def serve(ctl: Controller) -> None:
    await asyncio.gather(*(srv.serve() for srv in build_servers(ctl)))


def build_app(ctl: Controller) -> Starlette:
    git_endpoint = make_git_endpoint(
        ctl.git, lambda request: ctl.git_grant(bearer(request.headers.get("authorization", ""))))
    oauth_token = make_token_endpoint(ctl.identity, ctl.sandbox_binding)
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
        return FileResponse(STATIC / "index.html")

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
        return JSONResponse(ctl.list_changes())

    @guarded
    async def state(request: Request):
        return JSONResponse(ctl.get(request.path_params["cid"]).snapshot())

    @guarded
    async def events(request: Request):
        ce = ctl.get(request.path_params["cid"])
        return JSONResponse(ce.st.events(after=int(request.query_params.get("after", 0)), limit=300))

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
        Route("/api/meta", meta),
        Route("/api/changes", changes, methods=["GET", "POST"]),
        Route("/api/changes/{cid}/state", state),
        Route("/api/changes/{cid}/events", events),
        Route("/api/changes/{cid}/nodes/{node_id}/action", action, methods=["POST"]),
        Route("/api/changes/{cid}/action", change_action, methods=["POST"]),
        Route("/api/stream", stream),
        Route("/api/docs", docs),
        Route("/api/doc", doc),
        Route("/api/runs/{run_id}/log", run_log),
        Route("/api/workflow", workflow),
        Route("/git/{path:path}", git_endpoint, methods=["GET", "POST"]),
        Route("/oauth/token", oauth_token, methods=["POST"]),
    ], lifespan=lifespan)
