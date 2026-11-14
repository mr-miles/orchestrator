"""MCP tools exposed to agents. Every call is authenticated by the run's bearer token.

Document paths may be given relative to the run's change (``plan/spec.md``) or fully qualified
(``changes/<id>/plan/spec.md``); returned paths and attestations are always fully qualified.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from . import attest
from .docstore import DocStoreError
from .engine import ChangeEngine, Controller, WorkflowError
from .model import Run

INSTRUCTIONS = """Legion orchestrator tools. Start with get_assignment(). Documents are immutable
and versioned (docs_write creates a new version; pass base_version when revising). Paths are
relative to your change unless they start with changes/. Back every claim with attestations:
file:<path>@<version>, git:<repo>/<path>@<sha> or git:<repo>@<sha>. Finish with exactly one
accepted report(status="DONE"|"BLOCKED"). Rejected reports list violated guard ids."""

AGENT_HIDDEN = ("/control/logs/",)


def _hidden(path: str) -> bool:
    return any(h in "/" + path for h in AGENT_HIDDEN)


def build_mcp(ctl: Controller) -> MCPServer:
    server = MCPServer("legion", instructions=INSTRUCTIONS)

    def current(ctx: Context) -> tuple[ChangeEngine, Run]:
        req = ctx.request_context.request
        auth = req.headers.get("authorization", "") if req is not None else ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        found = ctl.run_for_token(token)
        if found is None:
            raise PermissionError("invalid or expired run token (runs end when their report is accepted)")
        return found

    def readable(ce: ChangeEngine, path: str) -> str:
        path = ce.p(path)
        if _hidden(path):
            raise ValueError(f"{path} is not readable by agents")
        return path

    @server.tool(description="Identify this run: id, role, change, node, cycle, write scope.")
    async def whoami(ctx: Context) -> dict:
        ce, r = current(ctx)
        return {"run": r.id, "change": ce.cid, "role": r.role, "node": r.node_id, "cycle": r.cycle,
                "soul": r.soul, "write_scope": r.write_scope, "required_docs": r.required_docs,
                "doc_root": ce.p(""), "prompt_pin": r.prompt_pin}

    @server.tool(description="Full assignment: task contract, context attestations, feedback, git.")
    async def get_assignment(ctx: Context) -> dict:
        ce, r = current(ctx)
        a = dict(r.assignment)
        a.update({"run": r.id, "change": ce.cid, "doc_root": ce.p(""), "role": r.role,
                  "node": r.node_id, "write_scope": r.write_scope,
                  "required_docs": r.required_docs, "context": r.context_refs,
                  "required_git": ctl.assets.roles[r.role].git,
                  "report_schema": ctl.assets.roles[r.role].report_schema,
                  "git": {"url": ctl.git_url, "branch": r.branch, "base_sha": r.base_sha,
                          "checkout_sha": r.judged_sha or r.base_sha,
                          "can_push": r.role in ("implementer", "oracle")},
                  "attempts_left": ctl.cfg.limits.max_report_attempts - r.attempts})
        return a

    @server.tool(description="List documents (latest version of each) under a path prefix. "
                             "Relative prefixes are inside this change.")
    async def docs_list(ctx: Context, prefix: str = "") -> list[dict]:
        ce, _ = current(ctx)
        return [d for d in ctl.docs.list(ce.p(prefix)) if not _hidden(d["path"])]

    @server.tool(description="Read a document version (latest if version omitted).")
    async def docs_read(ctx: Context, path: str, version: int | None = None) -> dict:
        ce, _ = current(ctx)
        try:
            d = ctl.docs.read(readable(ce, path), version)
        except DocStoreError as e:
            raise ValueError(str(e))
        return d.meta() | {"content": d.content}

    @server.tool(description="All versions of a document with author, role and sha256.")
    async def docs_history(ctx: Context, path: str) -> list[dict]:
        ce, _ = current(ctx)
        return ctl.docs.history(readable(ce, path))

    @server.tool(description="Write a new immutable version of a document. base_version must equal "
                             "the current latest version (omit for a new document). Returns the "
                             "attestation file:<path>@<version> to cite in your report.")
    async def docs_write(ctx: Context, path: str, content: str, base_version: int | None = None) -> dict:
        ce, r = current(ctx)
        try:
            async with ctl.lock:
                v = ce.docs_write(r, path, content, base_version)
                ce._save()
        except (DocStoreError, WorkflowError) as e:
            raise ValueError(str(e))
        return v.meta()

    @server.tool(description="The schema a document path is bound to (name, format, definition). "
                             "Every write is validated against it; check before writing.")
    async def docs_schema(ctx: Context, path: str) -> dict:
        ce, _ = current(ctx)
        name = ctl.docs.schemas.bound(ce.p(path))
        s = ctl.docs.schemas.get(name)
        return {"path": ce.p(path), "schema": name, "format": s.fmt, "sha": s.sha, "definition": s.body}

    @server.tool(description="A schema by name, e.g. your role's report schema "
                             "(get_assignment().report_schema).")
    async def schema_get(ctx: Context, name: str) -> dict:
        current(ctx)
        try:
            s = ctl.docs.schemas.get(name)
        except Exception as e:
            raise ValueError(str(e))
        return {"schema": name, "format": s.fmt, "sha": s.sha, "definition": s.body}

    @server.tool(description="Check whether an attestation resolves.")
    async def verify_attestation(ctx: Context, ref: str) -> dict:
        ce, _ = current(ctx)
        ref = attest.normalize(ref, ce.cid)
        try:
            a = attest.parse(ref, [ctl.cfg.repo.name])
        except attest.AttestationError as e:
            return {"ref": ref, "valid": False, "error": str(e)}
        return {"ref": ref, "valid": await attest.resolve(a, ctl.docs, ctl.git)}

    @server.tool(description="Report progress (shown live to the operator).")
    async def progress(ctx: Context, message: str, percent: float | None = None) -> dict:
        ce, r = current(ctx)
        ce.progress(r, message, percent)
        return {"ok": True}

    @server.tool(description="Submit the result of this run. status is DONE or BLOCKED. "
                             "attestations: list of file:/git: references. data: role-specific "
                             "structured result (verdict, findings, checked, results). BLOCKED needs "
                             "blocked_reason and missing_decision. Returns accepted or violations.")
    async def report(ctx: Context, status: str, summary: str, attestations: list[str],
                     data: dict[str, Any] | str | None = None, blocked_reason: str | None = None,
                     missing_decision: str | None = None) -> dict:
        ce, r = current(ctx)
        if isinstance(data, str):
            try:
                data = json.loads(data) if data.strip() else None
            except json.JSONDecodeError as e:
                return {"accepted": False, "violations": [f"data is not valid JSON: {e}"]}
        if data is not None and not isinstance(data, dict):
            return {"accepted": False, "violations": ["data must be a JSON object"]}
        return await ce.handle_report(r.id, status, summary, attestations, data,
                                      blocked_reason, missing_decision)

    return server
