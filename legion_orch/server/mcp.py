"""The MCP server agents connect to: store tools + workflow tools on one endpoint.

Every call carries a bearer that the OpenShell gateway obtained for the calling sandbox with its
SPIFFE identity (see identity.py); it is bound to the sandbox's live run per request. The
sandbox itself holds no credential. The document tools come from
:mod:`legion_orch.store.mcp`; this module supplies the orchestrator's session policy for them:

* paths are relative to the run's change (``plan/spec.md``) unless they start with ``changes/``;
  returned paths and attestations are fully qualified;
* agents may read every change except ``*/control/logs/``;
* writes go through :meth:`ChangeEngine.docs_write` (write scope, controller-owned paths, G-W1),
  then the store's own checks (G-W2, G-W3).

Refusals are raised as :class:`ToolError` so the agent sees the reason; the MCP SDK hides the
message of any other exception.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..store import DocStoreError, DocVersion
from ..store.mcp import STORE_TOOLS, StoreSession, register_store_tools
from ..util import bearer
from ..workflow import attest
from ..workflow.engine import ChangeEngine, Controller, WorkflowError
from ..workflow.model import Run

INSTRUCTIONS = """Legion orchestrator tools. Start with get_assignment(). Documents are immutable,
versioned and schema-validated (docs_write creates a new version; pass base_version when revising;
docs_schema(path) shows what a path must contain). Paths are relative to your change unless they
start with changes/. Back every claim with attestations: file:<path>@<version>,
git:<repo>/<path>@<sha> or git:<repo>@<sha>. Finish with exactly one accepted
report(status="DONE"|"BLOCKED"). Rejected reports list violated guard ids."""

AGENT_HIDDEN = ("/control/logs/",)
WORKFLOW_TOOLS = ("whoami", "get_assignment", "verify_attestation", "progress", "report")
# The gateway's MCP allowlist (sandbox network policy) is generated from exactly this list.
MCP_TOOLS = tuple(STORE_TOOLS) + WORKFLOW_TOOLS


def _hidden(path: str) -> bool:
    return any(h in "/" + path for h in AGENT_HIDDEN)


def build_mcp(ctl: Controller) -> MCPServer:
    server = MCPServer("legion", instructions=INSTRUCTIONS)

    def current(ctx: Context) -> tuple[ChangeEngine, Run]:
        req = ctx.request_context.request
        found = ctl.run_for_bearer(bearer(req.headers.get("authorization", "")) if req else None)
        if found is None:
            raise ToolError("no live run for this sandbox identity (runs end when their report is "
                            "accepted; check the legion-controller provider is attached)")
        return found

    # ------------------------------------------------------------------ store tools
    def store_session(ctx: Context) -> StoreSession:
        ce, run = current(ctx)

        async def write(path: str, content: str, base_version: int | None) -> DocVersion:
            async with ctl.lock:
                try:
                    v = ce.docs_write(run, path, content, base_version)  # G-W1 → store G-W2/G-W3
                except (WorkflowError, DocStoreError) as e:
                    raise ToolError(str(e)) from None
                ce._save()
                return v

        return StoreSession(docs=ctl.docs, resolve=ce.p, readable=lambda p: not _hidden(p),
                            write=write)

    register_store_tools(server, store_session)

    # ------------------------------------------------------------------ workflow tools
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

    @server.tool(description="Check whether an attestation (file: or git:) resolves.")
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
                             "structured result matching your report_schema. BLOCKED needs "
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

    registered = {t.name for t in server._tool_manager.list_tools()}
    if registered != set(MCP_TOOLS):   # the gateway's MCP allowlist must match exactly
        raise RuntimeError(f"MCP tools {sorted(registered)} != policy allowlist {sorted(MCP_TOOLS)}")
    ctl.mcp_tools = list(MCP_TOOLS)    # used when rendering sandbox network policies
    return server
