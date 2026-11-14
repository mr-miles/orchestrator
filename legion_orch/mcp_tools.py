"""MCP tools exposed to agents. Every call is authenticated by the run's bearer token."""
from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from . import attest
from .docstore import DocStoreError
from .engine import Engine, WorkflowError
from .model import Run

INSTRUCTIONS = """Legion orchestrator tools. Start with get_assignment(). Documents are immutable
and versioned (docs_write creates a new version; pass base_version when revising). Back every claim
with attestations: file:<path>@<version>, git:<repo>/<path>@<sha> or git:<repo>@<sha>. Finish with
exactly one accepted report(status="DONE"|"BLOCKED"). Rejected reports list violated guard ids."""


def build_mcp(engine: Engine) -> MCPServer:
    server = MCPServer("legion", instructions=INSTRUCTIONS)

    def current(ctx: Context) -> Run:
        req = ctx.request_context.request
        auth = req.headers.get("authorization", "") if req is not None else ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        run = engine.run_for_token(token) if token else None
        if run is None:
            raise PermissionError("invalid or expired run token (runs end when their report is accepted)")
        return run

    @server.tool(description="Identify this run: id, role, node, cycle, write scope.")
    async def whoami(ctx: Context) -> dict:
        r = current(ctx)
        return {"run": r.id, "role": r.role, "node": r.node_id, "cycle": r.cycle, "soul": r.soul,
                "write_scope": r.write_scope, "required_docs": r.required_docs,
                "prompt_pin": r.prompt_pin}

    @server.tool(description="Full assignment: task contract, context attestations, feedback, git.")
    async def get_assignment(ctx: Context) -> dict:
        r = current(ctx)
        a = dict(r.assignment)
        a.update({"run": r.id, "role": r.role, "node": r.node_id, "write_scope": r.write_scope,
                  "required_docs": r.required_docs, "context": r.context_refs,
                  "required_git": engine.assets.roles[r.role].git,
                  "git": {"url": engine.git_url, "branch": r.branch, "base_sha": r.base_sha,
                          "checkout_sha": r.judged_sha or r.base_sha,
                          "can_push": r.role in ("implementer", "oracle")},
                  "attempts_left": engine.cfg.limits.max_report_attempts - r.attempts})
        return a

    @server.tool(description="List documents (latest version of each) under a path prefix.")
    async def docs_list(ctx: Context, prefix: str = "") -> list[dict]:
        current(ctx)
        return engine.docs.list(prefix)

    @server.tool(description="Read a document version (latest if version omitted).")
    async def docs_read(ctx: Context, path: str, version: int | None = None) -> dict:
        current(ctx)
        try:
            d = engine.docs.read(path, version)
        except DocStoreError as e:
            raise ValueError(str(e))
        return d.meta() | {"content": d.content}

    @server.tool(description="All versions of a document with author, role and sha256.")
    async def docs_history(ctx: Context, path: str) -> list[dict]:
        current(ctx)
        return engine.docs.history(path)

    @server.tool(description="Write a new immutable version of a document. base_version must equal "
                             "the current latest version (omit for a new document). Returns the "
                             "attestation file:<path>@<version>.")
    async def docs_write(ctx: Context, path: str, content: str, base_version: int | None = None) -> dict:
        r = current(ctx)
        try:
            async with engine.lock:
                v = engine.docs_write(r, path, content, base_version)
                engine._save()
        except (DocStoreError, WorkflowError) as e:
            raise ValueError(str(e))
        return v.meta()

    @server.tool(description="Check whether an attestation resolves.")
    async def verify_attestation(ctx: Context, ref: str) -> dict:
        current(ctx)
        try:
            a = attest.parse(ref, [engine.cfg.repo.name])
        except attest.AttestationError as e:
            return {"ref": ref, "valid": False, "error": str(e)}
        return {"ref": ref, "valid": await attest.resolve(a, engine.docs, engine.git)}

    @server.tool(description="Report progress (shown live to the operator).")
    async def progress(ctx: Context, message: str, percent: float | None = None) -> dict:
        r = current(ctx)
        engine.progress(r, message, percent)
        return {"ok": True}

    @server.tool(description="Submit the result of this run. status is DONE or BLOCKED. "
                             "attestations: list of file:/git: references. data: role-specific "
                             "structured result (verdict, findings, checked, results). BLOCKED needs "
                             "blocked_reason and missing_decision. Returns accepted or violations.")
    async def report(ctx: Context, status: str, summary: str, attestations: list[str],
                     data: dict[str, Any] | str | None = None, blocked_reason: str | None = None,
                     missing_decision: str | None = None) -> dict:
        r = current(ctx)
        if isinstance(data, str):
            try:
                data = json.loads(data) if data.strip() else None
            except json.JSONDecodeError as e:
                return {"accepted": False, "violations": [f"data is not valid JSON: {e}"]}
        if data is not None and not isinstance(data, dict):
            return {"accepted": False, "violations": ["data must be a JSON object"]}
        return await engine.handle_report(r.id, status, summary, attestations, data,
                                          blocked_reason, missing_decision)

    return server
