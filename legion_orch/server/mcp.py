"""The MCP server agents connect to: store tools + workflow tools on one endpoint.

Every call carries a bearer that the OpenShell gateway obtained for the calling sandbox with its
SPIFFE identity (see identity.py); it is bound to the sandbox's live run per request. The
sandbox itself holds no credential. The document tools come from
:mod:`legion_orch.store.mcp`; this module supplies the orchestrator's session policy for them:

* paths are relative to the run's change (``plan/spec.md``) unless they start with ``changes/``;
  returned paths and attestations are fully qualified;
* agents may read every change except ``*/control/logs/``;
* writes go through :meth:`ChangeEngine.agent_write` (write scope, controller-owned paths, G-W1),
  then the store's own checks (G-W2, G-W3).

Refusals are raised as :class:`ToolError` so the agent sees the reason; the MCP SDK hides the
message of any other exception.
"""
from __future__ import annotations

import json
from typing import Any

from mcp import MCPError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import METHOD_NOT_FOUND

from ..git import ShaError
from ..store import DocCheck, DocStoreError, DocVersion
from ..store.mcp import STORE_TOOLS, StoreSession, register_store_tools
from ..util import bearer
from ..workflow import attest
from ..workflow.cards import card_id
from ..workflow.engine import ChangeEngine, Controller, WorkflowError
from ..workflow.model import Run
from ..workflow.results import ReportOutcome
from .a2a import A2AHub

INSTRUCTIONS = """Legion orchestrator tools. Start with inbox(): your work is the orchestrator's
task in it (its first message is your assignment), with any messages from other agents. Documents
are immutable,
versioned and schema-validated (docs_write creates a new version; pass base_version when revising;
docs_schema(path) shows what a path must contain). Paths are relative to your change unless they
start with changes/. Back every claim with attestations: file:<path>@<version>,
git:<repo>/<path>@<sha> or git:<repo>@<sha>, with the full 40-character sha. File your results
with one accepted report(status="DONE"|"BLOCKED") – accepted, it ends your task; then end your turn. Rejected
reports list violated guard ids and use up an attempt; check first with report(..., dry_run=true)
and docs_write(..., dry_run=true), which use none. Answer the messages in your inbox with
a2a_reply()."""

AGENT_HIDDEN = ("/control/runs/",)
WORKFLOW_TOOLS = ("get_assignment", "verify_attestation", "progress", "report",
                  "inbox", "a2a_reply",
                  "verification_request", "verification_status", "verification_evidence",
                  "ticket_status", "workflow_context", "test_defect")
# The gateway's MCP allowlist (sandbox network policy) is generated from exactly this list.
MCP_TOOLS = tuple(STORE_TOOLS) + WORKFLOW_TOOLS


def _hidden(path: str) -> bool:
    return any(h in "/" + path for h in AGENT_HIDDEN)


# MCPServer always registers (empty) prompt and resource handlers and so advertises both
# capabilities (python-sdk issue #2473), which makes clients call methods the gateway policy
# does not allow. The server offers tools only: these are neither advertised nor served.
NOT_OFFERED = ("prompts", "resources")
ADVERTISING = ("initialize", "server/discover")


async def tools_only(ctx, call_next):
    """Server middleware (the SDK's public hook): refuse prompts/* and resources/* as unknown
    methods, and drop their capabilities from the initialize / server/discover results."""
    if ctx.method.split("/", 1)[0] in NOT_OFFERED:
        raise MCPError(METHOD_NOT_FOUND, f"Method not found: {ctx.method}")
    result = await call_next(ctx)
    caps = result.get("capabilities") if ctx.method in ADVERTISING and isinstance(result, dict) else None
    if isinstance(caps, dict):
        for name in NOT_OFFERED:
            caps.pop(name, None)
    return result


async def build_mcp(ctl: Controller) -> MCPServer:
    def caller(req) -> tuple[ChangeEngine, Run] | None:
        """The live run behind a request's bearer, marked connected: any MCP message from its
        sandbox (initialize and tools/list included, which the agent CLI sends at start-up,
        long before its first tool call) shows the agent is up and reaches the controller."""
        found = ctl.run_for_bearer(bearer(req.headers.get("authorization", "")) if req else None)
        if found is not None:
            found[0].connected(found[1])
        return found

    async def mark_connected(ctx, call_next):
        caller(ctx.request)
        return await call_next(ctx)

    server = MCPServer("legion", instructions=INSTRUCTIONS, middleware=[mark_connected, tools_only])

    def current(ctx: Context) -> tuple[ChangeEngine, Run]:
        found = caller(ctx.request_context.request)
        if found is None:
            raise ToolError("no live run for this sandbox identity (runs end when their report is "
                            "accepted; check the legion-mcp provider is attached)")
        return found

    # ------------------------------------------------------------------ store tools
    def store_session(ctx: Context) -> StoreSession:
        ce, run = current(ctx)

        async def write(path: str, content: str, base_version: int | None,
                        dry_run: bool) -> DocVersion | DocCheck:
            try:
                return await ce.agent_write(run, path, content, base_version, dry_run)
            except (WorkflowError, DocStoreError) as e:
                raise ToolError(str(e)) from None

        return StoreSession(docs=ctl.docs, resolve=ce.p, readable=lambda p: not _hidden(p),
                            write=write)

    register_store_tools(server, store_session)

    # ------------------------------------------------------------------ workflow tools
    @server.tool(description="Who you are and what to do: your run, role, ticket and attempt, "
                             "its work, inputs and context attestations, the findings to fix, "
                             "the outputs to report, your write scope, git.")
    async def get_assignment(ctx: Context) -> dict:
        ce, r = current(ctx)
        a = dict(r.assignment)
        a.update({"run": r.id, "change": ce.cid, "doc_root": ce.p(""), "role": r.role,
                  "ticket": r.ticket, "attempt": r.attempt, "write_scope": r.write_scope,
                  "outputs": r.outputs, "context": r.context_refs,
                  "required_git": ctl.assets.roles[r.role].git,
                  "report_schema": ctl.assets.roles[r.role].report_schema,
                  "git": {"url": ce.git_url, "branch": r.branch, "base_sha": r.base_sha,
                          "checkout_sha": r.judged_sha or r.base_sha,
                          "can_push": r.branch is not None},   # only roles that push get one
                  "soul": r.soul, "prompt_pin": r.prompt_pin,
                  "attempts_left": ctl.cfg.limits.max_report_attempts - r.attempts})
        return a

    @server.tool(description="Check whether an attestation (file: or git:) resolves; `ref` is "
                             "its full form (a git sha in full, as reports and plans keep it).")
    async def verify_attestation(ctx: Context, ref: str) -> dict:
        ce, _ = current(ctx)
        ref = attest.normalize(ref, ce.cid)
        try:
            ref = await attest.expand(ref, ce.git)
            a = attest.parse(ref, [ce.workspace])
        except attest.AttestationError as e:
            return {"ref": ref, "valid": False, "error": str(e)}
        return {"ref": ref, "valid": await attest.resolve(a, ctl.docs, ce.git)}

    @server.tool(description="Report progress (shown live to the operator).")
    async def progress(ctx: Context, message: str, percent: float | None = None) -> dict:
        ce, r = current(ctx)
        ce.progress(r, message, percent)
        return {"ok": True}

    @server.tool(description="Submit the result of this run. status is DONE or BLOCKED. "
                             "attestations: list of file:/git: references. outputs: each output "
                             "your role names (get_assignment) -> the document it is, as "
                             "file:<path>@<version> (a list for a list output). data: "
                             "role-specific structured result matching your report_schema. "
                             "BLOCKED needs blocked_reason and missing_decision. Returns "
                             "accepted (then end your turn: that hands your work back) or "
                             "violations. dry_run=true runs every check without submitting or "
                             "using up an attempt: it returns would_accept and the violations.")
    async def report(ctx: Context, status: str, summary: str, attestations: list[str],
                     data: dict[str, Any] | str | None = None, blocked_reason: str | None = None,
                     missing_decision: str | None = None, dry_run: bool = False,
                     outputs: dict[str, Any] | None = None) -> dict:
        ce, r = current(ctx)
        if isinstance(data, str):
            try:
                data = json.loads(data) if data.strip() else None
            except json.JSONDecodeError as e:
                return ReportOutcome.reject(f"data is not valid JSON: {e}").model_dump(
                    mode="json", exclude_none=True)
        if data is not None and not isinstance(data, dict):
            return ReportOutcome.reject("data must be a JSON object").model_dump(
                mode="json", exclude_none=True)
        outcome = await ce.handle_report(r.id, status, summary, attestations, data,
                                         blocked_reason, missing_decision, dry_run, outputs)
        out = outcome.model_dump(mode="json", exclude_none=True)
        if outcome.accepted and not dry_run:          # what still waits for an answer from it
            out["inbox"] = len(A2AHub.of(ctl).inbox.pending(ce.cid, card_id(r.role, r.ticket)))
        return out

    @server.tool(description="The oracle only, while answering a message: confirm that some of "
                             "your protected tests are not faithful to their criteria (the test's "
                             "implementation is wrong, not the code). tests: their paths; "
                             "reason: what the test does wrong and what it must check instead to "
                             "be faithful to its criterion. The controller adds a fix – your next "
                             "round, in your session, at the end of delivery – and a human signs "
                             "the revised tests. The criteria are fixed: never use it because a "
                             "criterion looks wrong, or to make a build pass.")
    async def test_defect(ctx: Context, tests: list[str], reason: str) -> dict:
        ce, r = current(ctx)
        try:
            return await ce.test_defect(r, tests, reason)
        except WorkflowError as e:
            raise ToolError(str(e)) from None

    # ------------------------------------------------------------------ where your work stands
    @server.tool(description="A ticket of your change as it is now – yours by default, or any "
                             "ticket id: its status and attempt (of its limit), what it waits "
                             "for, its criteria and their verdicts, its runs, the findings it "
                             "was told to fix, its outputs.")
    async def ticket_status(ctx: Context, ticket: str | None = None) -> dict:
        ce, r = current(ctx)
        try:
            return ce.ticket_status(ticket or r.ticket)
        except WorkflowError as e:
            raise ToolError(str(e)) from None

    @server.tool(description="How your ticket fits the change: the goal and tier, its parent and "
                             "the groups around it, what runs before and after it, its inputs "
                             "(the approved versions it consumes), who judges it, and whom to "
                             "ask (cards).")
    async def workflow_context(ctx: Context) -> dict:
        ce, r = current(ctx)
        try:
            return ce.workflow_context(r.ticket)
        except WorkflowError as e:
            raise ToolError(str(e)) from None

    # ------------------------------------------------------------------ verification service
    def _view(v) -> dict:
        res = v.result
        return {"id": v.id, "sha": v.sha, "state": str(v.state), "attempt": v.attempt,
                "commands": v.commands, "queued_at": v.queued_at, "started_at": v.started_at,
                "ended_at": v.ended_at,
                "results": [{"command": r.command, "exit_code": r.exit_code, "setup": r.setup}
                            for r in res.results] if res else [],
                "infra_error": res.infra_error if res else None,
                "attestation": res.attestation if res else None}

    async def _sha_of(ce: ChangeEngine, r: Run, sha: str | None) -> str:
        if sha:                            # in full: an unambiguous abbreviation expanded
            try:
                return await ce.git.expand_commit(sha)
            except ShaError as e:
                raise ToolError(str(e)) from None
        if r.branch:
            tip = await ce.git.ref_sha(f"refs/heads/{r.branch}")
            if tip:
                return tip
            raise ToolError(f"your branch {r.branch} is not pushed yet: push, then ask")
        return r.judged_sha or r.base_sha

    @server.tool(description="Verify your step's commands (the task's `verification`) at a commit "
                             "in a fresh verifier sandbox, as the controller will: sha defaults "
                             "to your pushed branch tip (judges: the commit you judge). Every "
                             "pushed tip is verified anyway, once per sha: this returns that "
                             "verification (queued, running, or its result) rather than running "
                             "it again. Poll verification_status; limited per run.")
    async def verification_request(ctx: Context, sha: str | None = None) -> dict:
        ce, r = current(ctx)
        try:
            return _view(await ce.request_verification(r, await _sha_of(ce, r, sha)))
        except WorkflowError as e:
            raise ToolError(str(e)) from None

    @server.tool(description="A verification's state (queued, running, passed, failed, "
                             "infra_error, cancelled) and each command's exit code: by id, or "
                             "the latest of your step's commands at sha (default: your pushed "
                             "tip). Passed and failed are final for that commit.")
    async def verification_status(ctx: Context, id: str | None = None,
                                  sha: str | None = None) -> dict:
        ce, r = current(ctx)
        if id:
            v = ce.change.verifications.get(id)
        else:
            try:
                v = ce.latest_verification(r, await _sha_of(ce, r, sha))
            except WorkflowError as e:
                raise ToolError(str(e)) from None
        if v is None:
            raise ToolError("no such verification (verification_request starts one)")
        return _view(v)

    @server.tool(description="A finished verification's evidence: every command with its exit "
                             "code and output (the last `lines` lines of each), as recorded.")
    async def verification_evidence(ctx: Context, id: str, lines: int = 200) -> dict:
        ce, _ = current(ctx)
        v = ce.change.verifications.get(id)
        if v is None:
            raise ToolError(f"unknown verification {id}")
        full = ce.verification_record(id)
        if full is None:
            return _view(v) | {"note": "not finished: no evidence yet"}
        lines = max(1, min(lines, 2000))
        return _view(v) | {"results": [
            {"command": x["command"], "exit_code": x["exit_code"], "setup": x.get("setup", False),
             "output": "\n".join(x.get("output", "").splitlines()[-lines:])}
            for x in full.get("results", [])]}

    # ------------------------------------------------------------------ the inbox (server/inbox.py)
    hub = A2AHub.of(ctl)

    @server.tool(description="Everything addressed to you: your work – the orchestrator's task "
                             "(task_id = your run id), its first message your assignment, then "
                             "the orchestrator's follow-ups – and messages other agents of the "
                             "change sent you over A2A, each waiting for your a2a_reply. Call it "
                             "first, and again at milestones and before you end your turn.")
    async def inbox(ctx: Context) -> dict:
        ce, r = current(ctx)
        return {"tasks": hub.agent_inbox(ce, r, await get_assignment(ctx))}

    @server.tool(description="Answer a task in your inbox with your text; returns what is still "
                             "in your inbox (`inbox`). state: completed (default), "
                             "input-required, failed, rejected. Your own work's task (task_id = "
                             "your run id) is ended by your accepted report; reply failed on it "
                             "only to give up. A message from another agent: completed answers it, "
                             "input-required asks them something back, rejected declines it "
                             "(say why). A question back (an agent you messaged asked you "
                             "something): your text continues that conversation.")
    async def a2a_reply(ctx: Context, task_id: str, text: str, state: str = "completed") -> dict:
        ce, r = current(ctx)
        return await hub.agent_reply(ce, r, task_id, text, state)

    # the gateway's MCP allowlist must match exactly what is served
    registered = {t.name for t in await server.list_tools()}
    if registered != set(MCP_TOOLS):
        raise RuntimeError(f"MCP tools {sorted(registered)} != policy allowlist {sorted(MCP_TOOLS)}")
    return server
