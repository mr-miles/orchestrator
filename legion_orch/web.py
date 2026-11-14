"""HTTP surface: web UI + JSON API, MCP endpoint (/mcp/), git smart-HTTP (/git/)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route

from .docstore import DocStoreError
from .engine import Engine, WorkflowError
from .githttp import make_git_endpoint
from .mcp_tools import build_mcp

STATIC = Path(__file__).parent / "static"


def build_app(engine: Engine) -> Starlette:
    mcp_app = build_mcp(engine).streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        host=engine.cfg.server.host)
    git_endpoint = make_git_endpoint(engine.git, engine.git_grant)
    op_token = engine.cfg.server.operator_token

    def operator(request: Request) -> None:
        if op_token and not secrets.compare_digest(request.headers.get("x-operator-token", ""), op_token):
            raise PermissionError("operator token required")

    async def index(_: Request):
        return FileResponse(STATIC / "index.html")

    async def state(_: Request):
        return JSONResponse(engine.snapshot())

    async def events(request: Request):
        after = int(request.query_params.get("after", 0))
        return JSONResponse(engine.st.events(after=after, limit=300))

    async def stream(request: Request):
        async def gen():
            last = -1
            while not await request.is_disconnected():
                if engine.version != last:
                    last = engine.version
                    yield f"data: {json.dumps({'version': last})}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    async def docs(request: Request):
        return JSONResponse(engine.docs.list(request.query_params.get("prefix", "")))

    async def doc(request: Request):
        path = request.query_params["path"]
        ver = request.query_params.get("version")
        try:
            d = engine.docs.read(path, int(ver) if ver else None)
        except DocStoreError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse(d.meta() | {"content": d.content, "history": engine.docs.history(path)})

    async def run_log(request: Request):
        rid = request.path_params["run_id"]
        if rid not in engine.runs:
            return PlainTextResponse("unknown run", status_code=404)
        return PlainTextResponse(engine.log_tail(rid, int(request.query_params.get("lines", 300))))

    async def run_prompt(request: Request):
        rid = request.path_params["run_id"]
        p = engine.cfg.state_dir / "runs" / rid / "payload"
        if rid not in engine.runs or not p.exists():
            return JSONResponse({"error": "unknown run"}, status_code=404)
        return JSONResponse({"soul": (p / "soul.md").read_text(), "prompt": (p / "prompt.md").read_text(),
                             "pin": engine.runs[rid].prompt_pin})

    async def workflow(_: Request):
        doc = engine.cfg.workflow_doc
        text = doc.read_text() if doc.is_file() else "(WORKFLOW.md not found)"
        return PlainTextResponse(text, media_type="text/markdown")

    async def start_change(request: Request):
        try:
            operator(request)
            body = await request.json()
            ch = await engine.start_change(body.get("goal", ""))
            return JSONResponse({"ok": True, "change": ch.id})
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except WorkflowError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    async def action(request: Request):
        try:
            operator(request)
            body = await request.json()
            res = await engine.human_action(request.path_params["node_id"], body.get("action", ""),
                                            body.get("approver", ""), body.get("payload") or {})
            return JSONResponse(res)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except WorkflowError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(app):
            await engine.start()
            try:
                yield
            finally:
                await engine.stop()

    return Starlette(routes=[
        Route("/", index),
        Route("/api/state", state),
        Route("/api/events", events),
        Route("/api/stream", stream),
        Route("/api/docs", docs),
        Route("/api/doc", doc),
        Route("/api/runs/{run_id}/log", run_log),
        Route("/api/runs/{run_id}/prompt", run_prompt),
        Route("/api/workflow", workflow),
        Route("/api/change", start_change, methods=["POST"]),
        Route("/api/nodes/{node_id}/action", action, methods=["POST"]),
        Route("/git/{path:path}", git_endpoint, methods=["GET", "POST"]),
        Mount("/mcp", app=mcp_app),
    ], lifespan=lifespan)
