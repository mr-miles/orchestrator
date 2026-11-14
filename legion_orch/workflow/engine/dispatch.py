"""Dispatch: prepare runs, render prompts, payloads and policies, launch sandboxes.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...sandbox import (
    RUN_AGENT_SH,
    SandboxSpec,
    render_policy,
)
from ...util import now_iso
from .. import governance
from ..assets import AssetError
from ..model import (
    ACCEPTED,
    BUILD,
    H_REQUIREMENTS,
    LIVE_RUN,
    ORACLE,
    PLAN,
    R_ACTIVE,
    R_STARTING,
    REWORK,
    RUNNING,
    Node,
    Oracle,
    Resume,
    Run,
)
from ..roles import handler_for
from .common import PLAN_DOCS, _slug


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


class DispatchMixin:
    def _approval_fresh(self) -> list[str]:
        """Guard G-B1: every H-2 subject (plan, each delta spec, oracle) approved and fresh."""
        return governance.stale_approvals(self)

    def _plan_docs(self) -> list[str]:
        return PLAN_DOCS + [f"plan/specs/{r}.md" for r in self.change.requirements]

    def oracle_commands(self) -> list[str]:
        orc = self.change.oracle or Oracle()
        cmds = orc.acceptance_commands + orc.integration_commands \
            + governance.surface_commands(self)
        return list(dict.fromkeys(cmds))

    def _context_refs(self, node: Node, role: str) -> list[str]:
        """Common context plus the role handler's own (workflow/roles/)."""
        h = handler_for(role)
        refs: list[str] = []
        appr1 = self.change.approvals.get(H_REQUIREMENTS)
        if appr1 and h.sees_requirements:
            refs += [f"file:{p}@{v}" for p, v in appr1.pins.items()]
        if h.sees_draft_plan or not self.change.approvals.get("oracle"):
            if self.nodes.get(PLAN) and self.nodes[PLAN].status == ACCEPTED:
                refs += [a for a in (self.latest_att(p) for p in self._plan_docs()) if a]
        else:
            for key, appr in self.change.approvals.items():
                if key == "oracle" or key.startswith("spec:"):
                    refs += [f"file:{p}@{v}" for p, v in appr.pins.items()]
        refs += h.extra_context(self, node)
        if self.change.oracle_sha and h.sees_oracle_commit:
            refs.append(f"git:{self.cfg.repo.name}@{self.change.oracle_sha}")
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

    def _prepare(self, node: Node, role: str, judged_sha: str | None = None) -> Run | None:
        """Create a run record under the lock (guards G-D1, G-D2, G-D4, G-B1), or escalate the
        node and return None."""
        ch, h = self.change, handler_for(role)
        if h.needs_fresh_approval:
            stale = self._approval_fresh()
            if stale:
                self.escalate(node, "approval", "; ".join(stale), resume=Resume.REWORK)
                return None
        if node.kind == ORACLE or (node.kind == BUILD and not node.base_sha):
            node.base_sha = ch.integration_head
        if h.judges_head:
            judged_sha = ch.integration_head
        run_id = self.host.next_run_id()
        branch = f"legion/{ch.id}/{_slug(node.id)}" if h.pushes else f"legion/{ch.id}/{run_id}-{role}"
        asg = self._assignment(node, role, branch, judged_sha)
        try:
            rendered = self.assets.render_role(role, asg.variables)
        except AssetError as e:
            self.escalate(node, "assets", str(e),
                          resume=Resume.REWORK if node.status == REWORK else Resume.READY)
            return None
        run = Run(id=run_id, node_id=node.id, role=role, cycle=node.cycle,
                  soul=node.task.soul if node.task and role == "implementer"
                  else self.assets.roles[role].soul,
                  base_sha=node.base_sha or ch.base_sha, branch=branch, judged_sha=judged_sha,
                  prompt_pin=rendered.pin, assets_sha=self.assets.sha,
                  write_scope=asg.write_scope, required_docs=asg.required_docs,
                  context_refs=asg.context, assignment=asg.for_agent(node))
        self._place_in_sandbox(run, node)
        self._record_dispatch(run, node)
        self._payload(run, rendered)
        self.event("dispatch", f"{role} run {run_id} (cycle {run.cycle}, soul {run.soul}, "
                    f"pin {rendered.pin[7:19]})", node.id, run_id)
        return run

    def _assignment(self, node: Node, role: str, branch: str, judged_sha: str | None) -> Assignment:
        """The template variables, document scopes and context of a run (guard G-D2 binds them)."""
        ch, task = self.change, node.task
        task_json = task.model_dump(mode="json") if task else {}
        variables = {
            "change_id": ch.id, "goal": ch.goal, "tier": ch.tier or "unassessed",
            "repo_name": self.cfg.repo.name,
            "base_sha": node.base_sha or ch.base_sha, "head_sha": ch.integration_head,
            "branch": branch, "task_id": task.id if task else node.id,
            "task_title": task.title if task else node.title,
            "task_json": json.dumps(task_json, indent=2),
            "cycle": node.cycle, "role": role, "candidate_sha": judged_sha or "",
            "workdir": "/sandbox/work", "task_soul": task.soul if task else "",
        }
        write, required = self.assets.role_paths(role, variables)
        write, required = [self.p(w) for w in write], [self.p(d) for d in required]
        context = self._context_refs(node, role)
        variables.update({
            "write_scope": ", ".join(f"`{w}`" for w in write),
            "required_docs": ", ".join(f"`{d}`" for d in required) or "(none)",
            "context_refs": "\n".join(f"- {c}" for c in context) or "(none)",
            "feedback": self._feedback_text(node),
        })
        return Assignment(variables, write, required, context, task_json)

    def _place_in_sandbox(self, run: Run, node: Node) -> None:
        """Guard G-D4: judges always get a fresh sandbox; only roles that reuse theirs keep it
        across the node's cycles."""
        h = handler_for(run.role)
        if h.reuses_sandbox and node.sandbox:
            run.sandbox = node.sandbox
            run.sandbox_ref = next((r.sandbox_ref for r in self.runs.values()
                                    if r.sandbox == node.sandbox and r.sandbox_ref), None)
        else:
            run.sandbox = f"lg-{self.change.id}-{run.id}".lower()[:63]
        if h.judge and node.current_run:
            judged = self.runs.get(node.current_run)
            if judged and (judged.sandbox == run.sandbox or judged.id == run.id):
                raise RuntimeError("G-D4 violated: judge would share a sandbox with judged run")

    def _record_dispatch(self, run: Run, node: Node) -> None:
        h, ch = handler_for(run.role), self.change
        self.runs[run.id] = run
        node.runs.append(run.id)
        if not h.panel:                 # panel members run alongside; the node stays REVIEWING
            node.current_run = run.id
            node.status = RUNNING
        if h.reuses_sandbox and node.sandbox is None:
            node.sandbox = run.sandbox
        if node.kind == BUILD and h.pushes and ch.first_build_at is None:
            ch.first_build_at = now_iso()          # approved_spec_and_oracle compares with this

    def _run_dir(self, run_id: str) -> Path:
        """Scratch only: payload (deleted after upload) and the live agent log."""
        return self.svc.tmp / "runs" / run_id

    def _policy(self, dest: Path, *, agent: bool, push: bool) -> Path:
        """Sandbox policy from policies/{agent,verifier}-policy.template.yaml: MCP vetted by the
        gateway (protocol: mcp, tool allowlist), git push only if the role may push."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.svc.mcp_tools is None:
            raise RuntimeError("MCP server not built: no tool allowlist for the sandbox policy")
        dest.write_text(render_policy(self.cfg, agent=agent, push=push,
                                      mcp_tools=self.svc.mcp_tools))
        return dest

    def _payload(self, run: Run, rendered) -> None:
        d = self._run_dir(run.id) / "payload"
        d.mkdir(parents=True, exist_ok=True)
        (d / "soul.md").write_text(rendered.soul)
        (d / "prompt.md").write_text(rendered.prompt)
        # No credential: the OpenShell gateway injects the bearer for the controller endpoint.
        # With the local runner the bearer is in the process env (gateway emulation) and Claude
        # Code expands ${LEGION_ACCESS_TOKEN} in the MCP config at start-up.
        server = {"type": "http", "url": self.mcp_url}
        if self.cfg.identity.dev_spire:
            server["headers"] = {"Authorization": "Bearer ${LEGION_ACCESS_TOKEN}"}
        (d / "mcp.json").write_text(json.dumps({"mcpServers": {"legion": server}}, indent=2))
        env = {
            "LEGION_RUN_ID": run.id, "LEGION_ROLE": run.role,
            "LEGION_BRANCH": run.branch, "LEGION_BASE_SHA": run.judged_sha or run.base_sha,
            "LEGION_GIT_URL": self.git_url, "LEGION_MCP_URL": self.mcp_url,
            "LEGION_NEEDS_REPO": "1" if self.assets.roles[run.role].repo else "0",
            "LEGION_AGENT_COMMAND": self.cfg.sandbox.agent_command,
        }
        (d / "run.env").write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items()))
        (d / "run-agent.sh").write_text(RUN_AGENT_SH)

    async def _launch(self, run_id: str) -> None:
        run = self.runs[run_id]
        payload = self._run_dir(run_id) / "payload"
        try:
            h = handler_for(run.role)
            reuse = h.reuses_sandbox and any(
                r.sandbox == run.sandbox and r.id != run.id for r in self.runs.values())
            if reuse:
                await self.runner.upload(run.sandbox, payload)
            else:
                policy = self._policy(self._run_dir(run_id) / "policy.yaml",
                                      agent=True, push=h.pushes)
                run.sandbox_ref = await self.runner.create(SandboxSpec(
                    name=run.sandbox, payload_dir=payload, policy_file=policy, agent=True,
                    change=self.cid, task=run.node_id, role=run.role, run=run.id))
            proc = await self.runner.start_agent(run.sandbox, self._run_dir(run_id) / "agent.log")
        except Exception as e:
            shutil.rmtree(payload, ignore_errors=True)
            async with self.lock:
                self._fail_run(run, f"sandbox launch failed: {e}")
                self._save()
            return
        shutil.rmtree(payload, ignore_errors=True)            # contains the run token
        (self._run_dir(run_id) / "policy.yaml").unlink(missing_ok=True)
        async with self.lock:
            self.procs[run_id] = proc
            if run.status == R_STARTING:
                run.status = R_ACTIVE
            self.event("sandbox", f"agent started in sandbox {run.sandbox}", run.node_id, run_id)
            self._save()
        rc = await proc.wait()
        async with self.lock:
            self.procs.pop(run_id, None)
            run.exit_code = rc
            if run.status in LIVE_RUN:
                self._fail_run(run, f"agent exited with code {rc} without an accepted report")
            else:
                self.event("agent", f"agent process exited ({rc})", run.node_id, run_id)
            self._archive_log(run)
            self._save()
