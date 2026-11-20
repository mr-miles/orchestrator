"""Agent runs: prepare a run (guards, prompt, payload, policy), launch its sandbox, await the
agent's turn. A run is work the :class:`.Dispatcher` runs (:class:`AgentWork`); its result is
the role handler's, or the failure's."""
from __future__ import annotations

import asyncio
import json
import shlex
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...config import agent_command
from ...sandbox import (
    AGENT_PREFIX,
    agentlog,
    payload_scripts,
)
from ...store import DocStoreError
from ...util import now_iso
from .. import attest, governance
from ..assets import AssetError
from ..model import (
    ACCEPTED,
    H_ENV,
    H_REQUIREMENTS,
    LIVE_RUN,
    PLAN,
    R_REPORTED,
    REWORK,
    FailureSource,
    Node,
    Resume,
    Run,
    RunStage,
)
from ..results import Escalate, PhaseResult
from ..roles import handler_for
from ..view import RoleContext
from .common import ENV_SPEC, PLAN_DOCS, SKILLS_DIR, _slug, write_skills
from .core import ChangeCore
from .provision import sandbox_env
from .runs import RunLifecycle
from .sandboxes import Sandboxes
from .steps import sandbox_plan

if TYPE_CHECKING:
    from .reports import Reports


@dataclass
class Assignment:
    """What a run is told: template variables, document scopes and context attestations."""
    variables: dict[str, Any]
    write_scope: list[str]
    required_docs: list[str]
    context: list[str]
    task: dict[str, Any]

    def for_agent(self, node: Node) -> dict[str, Any]:
        """The assignment returned by get_assignment() (the rendered list of context refs is
        replaced by the list itself)."""
        return {k: v for k, v in self.variables.items() if k != "context_refs"} | {
            "task": self.task, "context": self.context,
            "feedback_items": [f.model_dump(mode="json") for f in node.feedback]}


def _file_path(ref: str) -> str | None:
    """The document path of a ``file:<path>@<version>`` attestation (None for git refs)."""
    return ref[5:].rsplit("@", 1)[0] if ref.startswith("file:") and "@" in ref else None


def assets_record(sha: str) -> str:
    return f"control/assets/{sha.removeprefix('sha256:')[:16]}.json"


@dataclass
class AgentWork:
    """One run of a role's agent for a node, as work for the dispatcher (which runs it in a
    slot): prepared under the lock (the guards may stop it), its sandbox launched, the agent's
    turn awaited. ``still``: whether its step still wants it, checked before it is prepared;
    ``started``: called under the lock once the run is prepared."""
    runs: AgentRuns
    node: Node
    role: str
    judged_sha: str | None = None
    still: Callable[[], bool] = lambda: True
    started: Callable[[Run], None] | None = None

    async def run(self) -> Ended | None:
        """The run and what it means (an escalation without a run: a guard stopped it); None:
        the step no longer wants it, so nothing ran."""
        c = self.runs.c
        async with c.lock:
            if not self.still():
                return None
            prepared = self.runs.prepare(self.node, self.role, self.judged_sha)
            if isinstance(prepared, Escalate):
                return Ended(None, prepared)
            if self.started:
                self.started(prepared)
            c.save()
        return Ended(prepared, await self.runs.launch(prepared))


@dataclass
class Ended:
    """How an agent run ended: its run (None: it never started) and the phase's material – the
    role handler's result after an accepted report (None: nothing to decide yet), or the
    escalation of a failure."""
    run: Run | None
    result: PhaseResult | None


class AgentRuns:
    """Turns a node into a run of a role's agent (guards G-D1..G-D4, G-B1) and runs it: the
    sandbox, the agent's turn, its timers, and what its report means."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, sandboxes: Sandboxes,
                 roles: RoleContext):
        self.c, self.lifecycle, self.sandboxes, self.roles = core, lifecycle, sandboxes, roles
        self.reports: Reports = None  # type: ignore[assignment]   # set once it exists

    def _approval_fresh(self) -> list[str]:
        """Guard G-B1: every H-2 subject (plan, each delta spec, oracle) approved and fresh."""
        return governance.stale_approvals(self.c)

    def _plan_docs(self) -> list[str]:
        return PLAN_DOCS + [f"plan/specs/{r}.md" for r in self.c.change.requirements]

    def _context_refs(self, node: Node, role: str) -> list[str]:
        """Common context plus the role handler's own (workflow/roles/). A build task's roles get
        only its own requirements' delta specs; a document the controller gives (at its approved
        version) is not repeated at another version from the task's declared context."""
        h, cid = handler_for(role), self.c.cid
        refs: list[str] = []
        appr1 = self.c.change.approvals.get(H_REQUIREMENTS)
        if appr1 and h.sees_requirements:
            refs += [f"file:{p}@{v}" for p, v in appr1.pins.items()
                     if h.sees_risk or p != self.c.p("intake/risk.json")]
        if h.sees_draft_plan or not self.c.change.approvals.get(governance.approval_key("plan")):
            if (plan := self.c.current(PLAN)) and plan.status == ACCEPTED:
                refs += [a for a in (self.c.latest_att(p) for p in self._plan_docs()) if a]
        else:
            own = ({self.c.p(f"plan/specs/{r}.md") for r in node.task.requirements}
                   if h.task_scoped_specs and node.task else None)
            for key, appr in self.c.change.approvals.items():
                if key == "oracle" or key.startswith("spec:"):
                    refs += [f"file:{p}@{v}" for p, v in appr.pins.items()
                             if own is None or not p.startswith(self.c.p("plan/specs/")) or p in own]
        given = {_file_path(a) for a in refs} - {None}
        refs += [a for a in (attest.normalize(x, cid) for x in h.extra_context(self.roles, node))
                 if _file_path(a) not in given]
        if self.c.change.oracle_sha and h.sees_oracle_commit:
            refs.append(f"git:{self.c.cfg.repo.name}@{self.c.change.oracle_sha}")
        return list(dict.fromkeys(refs))

    def _feedback_text(self, node: Node) -> str:
        if not any(f.cycle >= node.round_start_cycle for f in node.feedback):
            return "(none)"
        lines, head = [], self.c.change.integration_head
        for f in node.feedback:
            if f.cycle < node.round_start_cycle:     # an earlier round's (a fix round starts fresh)
                continue
            atts = ", ".join(f.attestations)
            at = ""
            if f.head:
                at = (f" (given at integration head {f.head[:12]}, the current head)" if f.head == head
                      else f" (given at integration head {f.head[:12]}; the head is now {head[:12]})")
            lines.append(f"- cycle {f.cycle} [{f.source}] {f.text}" + at
                         + (f" (evidence: {atts})" if atts else ""))
        return "\n".join(lines)

    def prepare(self, node: Node, role: str, judged_sha: str | None = None) -> Run | Escalate:
        """Create a run record under the lock (guards G-D1, G-D2, G-D4, G-B1), or return why it
        cannot run."""
        ch, h = self.c.change, handler_for(role)
        # the blank repository's scaffold is built before H-2, on the critique's PASS (§5.3b)
        if h.needs_fresh_approval and not (node.task and node.task.scaffold):
            stale = self._approval_fresh()
            if stale:
                return Escalate("approval", "; ".join(stale), Resume.REWORK)
        if sandbox_env(ch, role) and (stale_env := governance.stale_environment(self.c)):
            return Escalate("approval", stale_env,
                            Resume.REWORK if node.status == REWORK else Resume.READY)
        plan = sandbox_plan(role)
        if plan.base_at_head(bool(node.base_sha)):
            node.base_sha = ch.integration_head
        if plan.at_head:
            judged_sha = ch.integration_head
        run_id = self.c.host.next_run_id()
        # only roles that push get a branch (one per node: a fix round's oracle is its own node,
        # oracle@n, so its branch starts afresh); the others check their commit out detached
        branch = f"legion/{ch.id}/{_slug(node.id)}" if plan.pushes else None
        asg = self._assignment(node, role, branch, judged_sha)
        try:
            rendered = self.c.assets.render_role(role, asg.variables)
        except AssetError as e:
            return Escalate("assets", str(e),
                            Resume.REWORK if node.status == REWORK else Resume.READY)
        run = Run(id=run_id, node_id=node.id, role=role, cycle=node.cycle,
                  soul=node.task.soul if node.task and role == "implementer"
                  else self.c.assets.roles[role].soul,
                  base_sha=node.base_sha or ch.base_sha, branch=branch, judged_sha=judged_sha,
                  prompt_pin=rendered.pin, assets_sha=self.c.assets.sha,
                  template_vars={k: str(v) for k, v in asg.variables.items()},
                  write_scope=asg.write_scope, required_docs=asg.required_docs,
                  context_refs=asg.context, assignment=asg.for_agent(node))
        self._record_assets()
        self._place_in_sandbox(run, node)
        self._record_dispatch(run, node)
        self._payload(run, rendered)
        self.c.event("dispatch", f"{role} run {run_id} (cycle {run.cycle}, soul {run.soul}, "
                     f"pin {rendered.pin[7:19]})", node.id, run_id)
        return run

    def _sandbox_tools(self, role: str) -> str:
        """What the role's sandbox has for the project, stated as a fact: its environment
        profile's tools, or – without a provisioned environment – the default image, to check."""
        ch = self.c.change
        env = ch.environment.roles.get(role) if ch.environment else None
        appr = ch.approvals.get(H_ENV)
        tools: list[str] = []
        if env is not None and appr is not None:
            try:
                spec = self.c.json_doc(ENV_SPEC, appr.pins.get(self.c.p(ENV_SPEC)))
                tools = list(spec["profiles"][env.profile]["tools"])
            except (KeyError, TypeError, ValueError, DocStoreError):
                tools = []
        if not tools:
            return ("the operator's default image: check a tool with `command -v` before relying "
                    "on it – do not assume python3, node or rsync; verification runs in the "
                    "verifier image, which may have less")
        return (f"profile `{env.profile}`: {', '.join(tools)}. Build, test and verify only with "
                "these (the verifier has them too); anything else on PATH, such as python3, is "
                "at most for your own edits, never for a verification command")

    def _assignment(self, node: Node, role: str, branch: str | None,
                    judged_sha: str | None) -> Assignment:
        """The template variables, document scopes and context of a run (guard G-D2 binds them)."""
        ch, task = self.c.change, node.task
        task_json = task.model_dump(mode="json") if task else {}
        variables = {
            "change_id": ch.id, "goal": ch.goal, "tier": ch.tier or "unassessed",
            "repo_name": self.c.cfg.repo.name,
            "base_sha": node.base_sha or ch.base_sha, "head_sha": ch.integration_head,
            "oracle_sha": ch.oracle_sha or "",
            "branch": branch or "(none: a detached checkout of the commit you work at)",
            "task_id": task.id if task else node.id,
            "task_title": task.title if task else node.title,
            "task_json": json.dumps(task_json, indent=2),
            "cycle": node.cycle, "role": role, "candidate_sha": judged_sha or "",
            "workdir": f"{self.c.cfg.sandbox.root}/work", "task_soul": task.soul if task else "",
            "fix_round": "", "scaffold": "", "sandbox_tools": self._sandbox_tools(role),
        } | handler_for(role).extra_variables(self.roles, node)
        write, required = self.c.assets.role_paths(role, variables)
        write, required = [self.c.p(w) for w in write], [self.c.p(d) for d in required]
        context = self._context_refs(node, role)
        variables.update({
            "write_scope": ", ".join(f"`{w}`" for w in write),
            "required_docs": ", ".join(f"`{d}`" for d in required) or "(none)",
            "context_refs": "\n".join(f"- {c}" for c in context) or "(none)",
            "feedback": self._feedback_text(node),
        })
        return Assignment(variables, write, required, context, task_json)

    def _record_assets(self) -> None:
        """Record which asset files (by hash) the change's runs used, once per asset set: a run's
        prompt can then be checked against the current files (Assets.check_prompt)."""
        path = assets_record(self.c.assets.sha)
        if not self.c.latest(path):
            self.c.write_record(path, {"sha": self.c.assets.sha, "files": self.c.assets.manifest()})

    def _place_in_sandbox(self, run: Run, node: Node) -> None:
        """Guard G-D4: judges always get a fresh sandbox; only roles that keep theirs reuse it
        across the node's cycles."""
        h, plan = handler_for(run.role), sandbox_plan(run.role)
        if plan.keeps and node.sandbox:
            run.sandbox = node.sandbox
            run.sandbox_ref = next((r.sandbox_ref for r in self.c.runs.values()
                                    if r.sandbox == node.sandbox and r.sandbox_ref), None)
        else:
            run.sandbox = self.c.host.new_sandbox_name(AGENT_PREFIX)
        if h.judge and node.current_run:
            judged = self.c.runs.get(node.current_run)
            if judged and (judged.sandbox == run.sandbox or judged.id == run.id):
                raise RuntimeError("G-D4 violated: judge would share a sandbox with judged run")

    def _record_dispatch(self, run: Run, node: Node) -> None:
        h, plan, ch = handler_for(run.role), sandbox_plan(run.role), self.c.change
        self.c.runs[run.id] = run
        node.runs.append(run.id)
        if not h.panel:                 # panel members run alongside; the node stays REVIEWING
            node.current_run = run.id
        if plan.keeps and node.sandbox is None:
            node.sandbox = run.sandbox
        # approved_spec_and_oracle compares with these (the scaffold precedes H-2, §5.3b)
        if node.task and plan.pushes and not node.task.scaffold:     # a build run
            if ch.first_build_at is None:
                ch.first_build_at = now_iso()
            rnd = next((x for x in ch.rounds if x.number == node.round), None)
            if rnd and rnd.first_build_at is None:
                rnd.first_build_at = now_iso()

    def _payload(self, run: Run, rendered) -> None:
        d = self.c.run_dir(run.id) / "payload"
        d.mkdir(parents=True, exist_ok=True)
        (d / "soul.md").write_text(rendered.soul)
        (d / "prompt.md").write_text(rendered.prompt)
        write_skills(d / SKILLS_DIR, rendered.skills)
        # No credential: the OpenShell gateway injects the bearer for the controller endpoint.
        # With the local runner the bearer is in the process env (gateway emulation) and Claude
        # Code expands ${LEGION_ACCESS_TOKEN} in the MCP config at start-up.
        # alwaysLoad: the controller's tools load at start, not deferred behind a tool search
        server = {"type": "http", "url": self.c.mcp_url, "alwaysLoad": True}
        if self.c.cfg.identity.dev_spire:
            server["headers"] = {"Authorization": "Bearer ${LEGION_ACCESS_TOKEN}"}
        (d / "mcp.json").write_text(json.dumps({"mcpServers": {"legion": server}}, indent=2))
        env = {
            "LEGION_RUN_ID": run.id, "LEGION_ROLE": run.role,
            "LEGION_GIT_URL": self.c.git_url, "LEGION_MCP_URL": self.c.mcp_url,
            "LEGION_AGENT_COMMAND": agent_command(self.c.cfg.sandbox),
        }
        if found := self._resumable_session(run):
            run.resumed_from, run.resumed_session = found
            env["LEGION_RESUME_SESSION"] = run.resumed_session
        (d / "run.env").write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items()))
        for name, text in payload_scripts().items():
            (d / name).write_text(text)
        for rel, text in handler_for(run.role).payload_files(self.roles, self.c.nodes[run.node_id],
                                                             run).items():
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)

    def _resumable_session(self, run: Run) -> tuple[str, str] | None:
        """For a role that keeps its session: the node's previous run in the same sandbox and
        its agent session, which this rework continues."""
        if not sandbox_plan(run.role).keeps_session:
            return None
        node = self.c.nodes[run.node_id]
        prev = [self.c.runs[r] for r in node.runs if r != run.id and r in self.c.runs]
        prev = [r for r in prev if r.role == run.role and r.sandbox == run.sandbox]
        if not prev:
            return None
        sid = prev[-1].session_id or agentlog.session_id(self.lifecycle.log_tail(prev[-1].id, 100_000))
        return (prev[-1].id, sid) if sid else None

    async def _stage(self, run: Run, stage: RunStage, message: str) -> None:
        async with self.c.lock:
            if run.status in LIVE_RUN and run.stage != RunStage.RUNNING:
                run.stage = stage
                self.c.event("sandbox", message, run.node_id, run.id)
                self.c.save()

    async def launch(self, run: Run) -> PhaseResult | None:
        """PREPARING (OpenShell creates the sandbox) -> LAUNCHING (payload in place, setup script
        and agent starting) -> RUNNING (set by the agent's first MCP call) -> EXITED. Returns
        once the agent's turn has ended: what its report means, or the failure's escalation."""
        run_id = run.id
        payload = self.c.run_dir(run_id) / "payload"
        try:
            plan = sandbox_plan(run.role)
            reuse = plan.keeps and any(
                r.sandbox == run.sandbox and r.id != run.id for r in self.c.runs.values())
            log_file = self.c.run_dir(run_id) / "agent.log"
            if reuse:
                async with self.sandboxes.lock(run.sandbox):
                    node = self.c.nodes.get(run.node_id)
                    if node and node.sandbox == run.sandbox and node.sandbox_stopped:
                        await self._stage(run, RunStage.PREPARING, f"starting sandbox {run.sandbox}")
                        await self.sandboxes.wake(run.sandbox)
                        node.sandbox_stopped = False
                    await self._stage(run, RunStage.LAUNCHING, f"reusing sandbox {run.sandbox}")
                    await self.sandboxes.upload(run.sandbox, payload)
                    await self.sandboxes.prepare(run, log_file, fresh=False)
                    proc = await self.sandboxes.start_agent(run.sandbox, log_file)
            else:
                await self._stage(run, RunStage.PREPARING, f"creating sandbox {run.sandbox}")
                env = sandbox_env(self.c.change, run.role)
                policy = self.c.svc.write_policy(self.c.run_dir(run_id) / "policy.yaml",
                                                 network=env.network if env else None,
                                                 agent=True)
                run.sandbox_ref = await self.sandboxes.create(run, payload, policy, push=plan.pushes)
                await self._stage(run, RunStage.LAUNCHING, f"sandbox {run.sandbox} created")
                await self.sandboxes.prepare(run, log_file, fresh=True)
                proc = await self.sandboxes.start_agent(run.sandbox, log_file)
        except Exception as e:
            shutil.rmtree(payload, ignore_errors=True)
            async with self.c.lock:
                if run.status in LIVE_RUN:
                    self.lifecycle.fail_run(run, f"sandbox launch failed: {e}", FailureSource.SYSTEM)
                self.c.save()
                return self.lifecycle.escalation(run) if run.failure else None
        shutil.rmtree(payload, ignore_errors=True)
        (self.c.run_dir(run_id) / "policy.yaml").unlink(missing_ok=True)
        async with self.c.lock:
            self.c.procs[run_id] = proc
            self.c.event("sandbox", f"agent process started in sandbox {run.sandbox}",
                         run.node_id, run_id)
            self.c.save()
        rc = await self._turn(run, proc)
        async with self.c.lock:
            self.c.procs.pop(run_id, None)
            run.exit_code = rc
            if run.status in LIVE_RUN:
                reason, source = self._exit_reason(run, rc)
                self.lifecycle.fail_run(run, reason, source, exit_code=rc)
            else:
                self.c.event("agent", f"agent process exited ({rc})", run.node_id, run_id)
            run.stage = RunStage.EXITED
            self.lifecycle.archive_log(run)
            # the agent's turn ended: control returns to the engine, with what its report means
            result = await self.reports.conclude(run)
            self.c.save()
        if sandbox_plan(run.role).keeps_session:
            await self.sandboxes.pause(run)
        return self.lifecycle.escalation(run) if run.status not in (R_REPORTED,) \
            and run.failure else result

    async def _turn(self, run: Run, proc) -> int:
        """Await the agent's process; its timers stop it (the run timeout while it is live, the
        end-of-turn grace after its accepted report). Each save may change which applies."""
        exited = asyncio.ensure_future(proc.wait())
        try:
            while not exited.done():
                saved = asyncio.ensure_future(self.c.next_save())
                try:
                    await asyncio.wait({exited, saved}, timeout=self.lifecycle.deadline(run),
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    saved.cancel()
                if not exited.done():
                    async with self.c.lock:
                        if self.lifecycle.stop_if_due(run):
                            self.c.save()
            return exited.result()
        finally:
            exited.cancel()

    def _exit_reason(self, run: Run, rc: int) -> tuple[str, FailureSource]:
        """Why a live run's process ended. The agent CLI's own result event means the agent ran
        and ended (an agent error); without one the process or its setup failed (a system
        error). A last turn that ended waiting on the agent's background tasks is not the end:
        the CLI should have resumed when they finished, so its exit is a system error."""
        log = self.lifecycle.log_tail(run.id, 2000)
        result = agentlog.agent_result(log)
        if result is not None and result.pending:
            n = len(result.pending)
            return (f"the agent CLI exited with code {rc} while the agent was waiting on {n} "
                    f"background task{'s' if n != 1 else ''} ({'; '.join(result.pending)[:300]}) "
                    "instead of resuming when they finished – check the agent image's CLI",
                    FailureSource.SYSTEM)
        if result is not None:
            if result.is_error:
                return f"the agent ended with an error ({result.subtype}): {result.message[:500]}", \
                    FailureSource.AGENT
            return (f"the agent finished without an accepted report: {result.message[:300]}",
                    FailureSource.AGENT)
        when = "after connecting" if run.stage == RunStage.RUNNING else "before connecting"
        last = agentlog.last_output(log)
        return (f"agent process exited with code {rc} {when}" + (f": {last}" if last else ""),
                FailureSource.SYSTEM)
