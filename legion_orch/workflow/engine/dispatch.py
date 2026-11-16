"""Dispatch: prepare runs, render prompts, payloads and policies, launch sandboxes."""
from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass
from typing import Any

from ...sandbox import (
    AGENT_PREFIX,
    RUN_AGENT_SH,
    SandboxSpec,
    agentlog,
    is_name_clash,
)
from ...util import now_iso
from .. import governance
from ..assets import AssetError
from ..environment import LibraryError
from ..model import (
    ACCEPTED,
    BUILD,
    H_REQUIREMENTS,
    LIVE_RUN,
    ORACLE,
    PLAN,
    REWORK,
    RUNNING,
    FailureSource,
    Node,
    Resume,
    Run,
    RunStage,
)
from ..roles import handler_for
from ..view import RoleContext
from .common import PLAN_DOCS, _slug
from .core import ChangeCore
from .runs import RunLifecycle


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


class Dispatcher:
    """Turns a ready node into a run (guards G-D1..G-D4, G-B1) and starts its agent."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, roles: RoleContext):
        self.c, self.lifecycle, self.roles = core, lifecycle, roles

    def _approval_fresh(self) -> list[str]:
        """Guard G-B1: every H-2 subject (plan, each delta spec, oracle) approved and fresh."""
        return governance.stale_approvals(self.c)

    def _plan_docs(self) -> list[str]:
        return PLAN_DOCS + [f"plan/specs/{r}.md" for r in self.c.change.requirements]

    def _context_refs(self, node: Node, role: str) -> list[str]:
        """Common context plus the role handler's own (workflow/roles/)."""
        h = handler_for(role)
        refs: list[str] = []
        appr1 = self.c.change.approvals.get(H_REQUIREMENTS)
        if appr1 and h.sees_requirements:
            refs += [f"file:{p}@{v}" for p, v in appr1.pins.items()]
        if h.sees_draft_plan or not self.c.change.approvals.get("oracle"):
            if self.c.nodes.get(PLAN) and self.c.nodes[PLAN].status == ACCEPTED:
                refs += [a for a in (self.c.latest_att(p) for p in self._plan_docs()) if a]
        else:
            for key, appr in self.c.change.approvals.items():
                if key == "oracle" or key.startswith("spec:"):
                    refs += [f"file:{p}@{v}" for p, v in appr.pins.items()]
        refs += h.extra_context(self.roles, node)
        if self.c.change.oracle_sha and h.sees_oracle_commit:
            refs.append(f"git:{self.c.cfg.repo.name}@{self.c.change.oracle_sha}")
        return list(dict.fromkeys(refs))

    def _feedback_text(self, node: Node) -> str:
        if not node.feedback:
            return "(none)"
        lines = []
        for f in node.feedback:
            atts = ", ".join(f.attestations)
            lines.append(f"- cycle {f.cycle} [{f.source}] {f.text}"
                         + (f" (evidence: {atts})" if atts else ""))
        return "\n".join(lines)

    def prepare(self, node: Node, role: str, judged_sha: str | None = None) -> Run | None:
        """Create a run record under the lock (guards G-D1, G-D2, G-D4, G-B1), or escalate the
        node and return None."""
        ch, h = self.c.change, handler_for(role)
        if h.needs_fresh_approval:
            stale = self._approval_fresh()
            if stale:
                self.c.escalate(node, "approval", "; ".join(stale), resume=Resume.REWORK)
                return None
        if self.c.sandbox_env(role) and (stale_env := self.c.environment_stale()):
            self.c.escalate(node, "approval", stale_env,
                            resume=Resume.REWORK if node.status == REWORK else Resume.READY)
            return None
        if node.kind == ORACLE or (node.kind == BUILD and not node.base_sha):
            node.base_sha = ch.integration_head
        if h.judges_head:
            judged_sha = ch.integration_head
        run_id = self.c.host.next_run_id()
        branch = f"legion/{ch.id}/{_slug(node.id)}" if h.pushes else f"legion/{ch.id}/{run_id}-{role}"
        asg = self._assignment(node, role, branch, judged_sha)
        try:
            rendered = self.c.assets.render_role(role, asg.variables)
        except AssetError as e:
            self.c.escalate(node, "assets", str(e),
                            resume=Resume.REWORK if node.status == REWORK else Resume.READY)
            return None
        run = Run(id=run_id, node_id=node.id, role=role, cycle=node.cycle,
                  soul=node.task.soul if node.task and role == "implementer"
                  else self.c.assets.roles[role].soul,
                  base_sha=node.base_sha or ch.base_sha, branch=branch, judged_sha=judged_sha,
                  prompt_pin=rendered.pin, assets_sha=self.c.assets.sha,
                  write_scope=asg.write_scope, required_docs=asg.required_docs,
                  context_refs=asg.context, assignment=asg.for_agent(node))
        self._place_in_sandbox(run, node)
        self._record_dispatch(run, node)
        self._payload(run, rendered)
        self.c.event("dispatch", f"{role} run {run_id} (cycle {run.cycle}, soul {run.soul}, "
                     f"pin {rendered.pin[7:19]})", node.id, run_id)
        return run

    def _assignment(self, node: Node, role: str, branch: str, judged_sha: str | None) -> Assignment:
        """The template variables, document scopes and context of a run (guard G-D2 binds them)."""
        ch, task = self.c.change, node.task
        task_json = task.model_dump(mode="json") if task else {}
        variables = {
            "change_id": ch.id, "goal": ch.goal, "tier": ch.tier or "unassessed",
            "repo_name": self.c.cfg.repo.name,
            "base_sha": node.base_sha or ch.base_sha, "head_sha": ch.integration_head,
            "branch": branch, "task_id": task.id if task else node.id,
            "task_title": task.title if task else node.title,
            "task_json": json.dumps(task_json, indent=2),
            "cycle": node.cycle, "role": role, "candidate_sha": judged_sha or "",
            "workdir": f"{self.c.cfg.sandbox.root}/work", "task_soul": task.soul if task else "",
            "environment_library": self._library_text(role),
        }
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

    def _library_text(self, role: str) -> str:
        """The library catalogue, for the environment role's prompt only."""
        if role != "environment" or self.c.cfg.environment is None:
            return "(not used by this role)"
        try:
            return self.c.environment_library().catalogue()
        except LibraryError as e:          # reported to the agent; its guard rejects the spec
            return f"(the library is invalid and an operator must fix it: {e})"

    def _place_in_sandbox(self, run: Run, node: Node) -> None:
        """Guard G-D4: judges always get a fresh sandbox; only roles that reuse theirs keep it
        across the node's cycles."""
        h = handler_for(run.role)
        if h.reuses_sandbox and node.sandbox:
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
        h, ch = handler_for(run.role), self.c.change
        self.c.runs[run.id] = run
        node.runs.append(run.id)
        if not h.panel:                 # panel members run alongside; the node stays REVIEWING
            node.current_run = run.id
            node.status = RUNNING
        if h.reuses_sandbox and node.sandbox is None:
            node.sandbox = run.sandbox
        if node.kind == BUILD and h.pushes and ch.first_build_at is None:
            ch.first_build_at = now_iso()          # approved_spec_and_oracle compares with this

    def _payload(self, run: Run, rendered) -> None:
        d = self.c.run_dir(run.id) / "payload"
        d.mkdir(parents=True, exist_ok=True)
        (d / "soul.md").write_text(rendered.soul)
        (d / "prompt.md").write_text(rendered.prompt)
        # No credential: the OpenShell gateway injects the bearer for the controller endpoint.
        # With the local runner the bearer is in the process env (gateway emulation) and Claude
        # Code expands ${LEGION_ACCESS_TOKEN} in the MCP config at start-up.
        server = {"type": "http", "url": self.c.mcp_url}
        if self.c.cfg.identity.dev_spire:
            server["headers"] = {"Authorization": "Bearer ${LEGION_ACCESS_TOKEN}"}
        (d / "mcp.json").write_text(json.dumps({"mcpServers": {"legion": server}}, indent=2))
        env = {
            "LEGION_RUN_ID": run.id, "LEGION_ROLE": run.role,
            "LEGION_BRANCH": run.branch, "LEGION_BASE_SHA": run.judged_sha or run.base_sha,
            "LEGION_GIT_URL": self.c.git_url, "LEGION_MCP_URL": self.c.mcp_url,
            "LEGION_NEEDS_REPO": "1" if self.c.assets.roles[run.role].repo else "0",
            "LEGION_AGENT_COMMAND": self.c.cfg.sandbox.agent_command,
        }
        (d / "run.env").write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items()))
        (d / "run-agent.sh").write_text(RUN_AGENT_SH)

    async def _stage(self, run: Run, stage: RunStage, message: str) -> None:
        async with self.c.lock:
            if run.status in LIVE_RUN and run.stage != RunStage.RUNNING:
                run.stage = stage
                self.c.event("sandbox", message, run.node_id, run.id)
                self.c.save()

    async def launch(self, run_id: str) -> None:
        """PREPARING (OpenShell creates the sandbox) -> LAUNCHING (payload in place, setup script
        and agent starting) -> RUNNING (set by the agent's first MCP call) -> EXITED."""
        run = self.c.runs[run_id]
        payload = self.c.run_dir(run_id) / "payload"
        try:
            h = handler_for(run.role)
            reuse = h.reuses_sandbox and any(
                r.sandbox == run.sandbox and r.id != run.id for r in self.c.runs.values())
            if reuse:
                await self._stage(run, RunStage.LAUNCHING, f"reusing sandbox {run.sandbox}")
                await self.c.runner.upload(run.sandbox, payload)
            else:
                await self._stage(run, RunStage.PREPARING, f"creating sandbox {run.sandbox}")
                policy = self.c.svc.write_policy(self.c.run_dir(run_id) / "policy.yaml",
                                                 agent=True, push=h.pushes)
                run.sandbox_ref = await self._create(run, payload, policy)
                await self._stage(run, RunStage.LAUNCHING, f"sandbox {run.sandbox} created")
            proc = await self.c.runner.start_agent(run.sandbox, self.c.run_dir(run_id) / "agent.log")
        except Exception as e:
            shutil.rmtree(payload, ignore_errors=True)
            async with self.c.lock:
                if run.status in LIVE_RUN:
                    self.lifecycle.fail_run(run, f"sandbox launch failed: {e}", FailureSource.SYSTEM)
                self.c.save()
            return
        shutil.rmtree(payload, ignore_errors=True)
        (self.c.run_dir(run_id) / "policy.yaml").unlink(missing_ok=True)
        async with self.c.lock:
            self.c.procs[run_id] = proc
            self.c.event("sandbox", f"agent process started in sandbox {run.sandbox}",
                         run.node_id, run_id)
            self.c.save()
        rc = await proc.wait()
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
            self.c.save()

    async def _create(self, run: Run, payload, policy, attempts: int = 3) -> str:
        """Create the run's sandbox. If the gateway already has a sandbox of that name (another
        controller on the same gateway), take a new name and try again."""
        for attempt in range(attempts):
            try:
                return await self.c.runner.create(SandboxSpec(
                    name=run.sandbox, payload_dir=payload, policy_file=policy, agent=True,
                    change=self.c.cid, task=run.node_id, role=run.role, run=run.id,
                    env=self.c.sandbox_env(run.role)))
            except Exception as e:
                if not is_name_clash(e) or attempt == attempts - 1:
                    raise
                old, run.sandbox = run.sandbox, self.c.host.new_sandbox_name(AGENT_PREFIX)
                node = self.c.nodes.get(run.node_id)
                if node and node.sandbox == old:
                    node.sandbox = run.sandbox
                self.c.event("sandbox", f"name {old} is taken on the gateway; using {run.sandbox}",
                             run.node_id, run.id)
        raise AssertionError("unreachable")

    def _exit_reason(self, run: Run, rc: int) -> tuple[str, FailureSource]:
        """Why a live run's process ended. The agent CLI's own result event means the agent ran
        and ended (an agent error); without one the process or its setup failed (a system
        error)."""
        log = self.lifecycle.log_tail(run.id, 2000)
        result = agentlog.agent_result(log)
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
