"""Dispatch: prepare runs, render prompts, payloads and policies, launch sandboxes.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

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
    Run,
)
from .common import JUDGE_ROLES, PANEL_ROLES, PLAN_DOCS, PUSH_ROLES, _slug


class DispatchMixin:
    def _approval_fresh(self) -> list[str]:
        """Guard G-B1: every H-2 subject (plan, each delta spec, oracle) approved and fresh."""
        return governance.stale_approvals(self)

    def _plan_docs(self) -> list[str]:
        return PLAN_DOCS + [f"plan/specs/{r}.md" for r in self.change.requirements]

    def _oracle_commands(self) -> list[str]:
        orc = self.change.oracle or {}
        cmds = orc.get("acceptance_commands", []) + orc.get("integration_commands", []) \
            + governance.surface_commands(self)
        return list(dict.fromkeys(cmds))

    def _context_refs(self, node: Node, role: str) -> list[str]:
        refs: list[str] = []
        appr1 = self.change.approvals.get(H_REQUIREMENTS)
        if appr1 and role != "intake":
            refs += [f"file:{p}@{v}" for p, v in appr1["pins"].items()]
        if role in ("critic", "oracle", "baseline") or not self.change.approvals.get("oracle"):
            if self.nodes.get(PLAN) and self.nodes[PLAN].status == ACCEPTED:
                refs += [a for a in (self.latest_att(p) for p in self._plan_docs()) if a]
        else:
            for key, appr in self.change.approvals.items():
                if key == "oracle" or key.startswith("spec:"):
                    refs += [f"file:{p}@{v}" for p, v in appr["pins"].items()]
        if node.kind == BUILD:
            refs += [c for c in node.task.get("context", []) if isinstance(c, str)]
            for d in node.depends_on:
                dn = self.nodes.get(d)
                if dn and dn.kind == BUILD and dn.accepted_sha:
                    refs.append(f"git:{self.cfg.repo.name}@{dn.accepted_sha}")
            if role in PANEL_ROLES:
                ev = self.latest_att(f"tasks/{node.task['id']}/evidence/cycle-{node.cycle}.md")
                if ev:
                    refs.append(ev)
                ver = node.verification.get(str(node.cycle))
                if ver and ver.get("attestation"):
                    refs.append(ver["attestation"])
        if role in ("integration", "release"):
            for b in self.build_nodes():
                refs += [a for a in (self.latest_att(f"tasks/{b.task['id']}/evidence/cycle-{b.cycle}.md"),)
                         if a]
            if role == "release":
                a = self.latest_att("integration/report.md")
                if a:
                    refs.append(a)
        if self.change.oracle_sha and role not in ("intake", "planner", "critic", "oracle"):
            refs.append(f"git:{self.cfg.repo.name}@{self.change.oracle_sha}")
        return list(dict.fromkeys(refs))

    def _feedback_text(self, node: Node) -> str:
        if not node.feedback:
            return "(none)"
        lines = []
        for f in node.feedback:
            atts = ", ".join(f.get("attestations") or [])
            lines.append(f"- cycle {f['cycle']} [{f['source']}] {f['text']}"
                         + (f" (evidence: {atts})" if atts else ""))
        return "\n".join(lines)

    def _prepare(self, node: Node, role: str, judged_sha: str | None = None) -> Run | None:
        """Create a run record under the lock (guards G-D1, G-D2, G-D4, G-B1)."""
        ch = self.change
        if role == "implementer":
            stale = self._approval_fresh()
            if stale:
                self._escalate(node, "approval", "; ".join(stale), resume=REWORK)
                return None
        if node.kind == BUILD and not node.base_sha:
            node.base_sha = ch.integration_head
        if node.kind in (ORACLE,):
            node.base_sha = ch.integration_head
        if role in ("baseline", "integration", "release"):
            judged_sha = ch.integration_head
        run_id = self.ctl.next_run_id()
        branch = f"legion/{ch.id}/{_slug(node.id)}" if role in PUSH_ROLES \
            else f"legion/{ch.id}/{run_id}-{role}"
        task = node.task or {}
        variables = {
            "change_id": ch.id, "goal": ch.goal, "tier": ch.tier or "unassessed",
            "repo_name": self.cfg.repo.name,
            "base_sha": node.base_sha or ch.base_sha, "head_sha": ch.integration_head,
            "branch": branch, "task_id": task.get("id", node.id), "task_title": task.get("title", node.title),
            "task_json": json.dumps(task, indent=2) if task else "{}",
            "cycle": node.cycle, "role": role, "candidate_sha": judged_sha or "",
            "workdir": "/sandbox/work", "task_soul": task.get("soul", ""),
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
        try:
            rendered = self.assets.render_role(role, variables)
        except AssetError as e:
            self._escalate(node, "assets", str(e), resume=node.status)
            return None
        run = Run(id=run_id, node_id=node.id, role=role,
                  soul=self.assets.roles[role].soul if role != "implementer" else task.get("soul"),
                  cycle=node.cycle,
                  base_sha=node.base_sha or ch.base_sha, branch=branch, judged_sha=judged_sha,
                  prompt_pin=rendered.pin, assets_sha=self.assets.sha,
                  write_scope=write, required_docs=required, context_refs=context,
                  assignment={k: v for k, v in variables.items()
                              if k not in ("context_refs",)} | {"task": task,
                                                                "context": context,
                                                                "feedback_items": node.feedback})
        # G-D4: judges always get a fresh sandbox; only implementers reuse theirs across cycles.
        if role == "implementer" and node.sandbox:
            run.sandbox = node.sandbox
            run.sandbox_ref = next((r.sandbox_ref for r in self.runs.values()
                                    if r.sandbox == node.sandbox and r.sandbox_ref), None)
        else:
            run.sandbox = f"lg-{ch.id}-{run_id}".lower()[:63]
        if role in JUDGE_ROLES and node.current_run:
            judged = self.runs.get(node.current_run)
            if judged and (judged.sandbox == run.sandbox or judged.id == run.id):
                raise RuntimeError("G-D4 violated: judge would share a sandbox with judged run")
        self.runs[run_id] = run
        node.runs.append(run_id)
        if role not in PANEL_ROLES:
            node.current_run = run_id
            node.status = RUNNING
        if role == "implementer":
            if node.sandbox is None:
                node.sandbox = run.sandbox
            if ch.first_build_at is None:
                ch.first_build_at = now_iso()
        self._payload(run, rendered)
        self._event("dispatch", f"{role} run {run_id} (cycle {run.cycle}, soul {run.soul}, "
                    f"pin {rendered.pin[7:19]})", node.id, run_id)
        return run

    def _run_dir(self, run_id: str) -> Path:
        """Scratch only: payload (deleted after upload) and the live agent log."""
        return self.ctl.tmp / "runs" / run_id

    def _policy(self, dest: Path, *, agent: bool, push: bool) -> Path:
        """Sandbox policy from policies/{agent,verifier}-policy.template.yaml: MCP vetted by the
        gateway (protocol: mcp, tool allowlist), git push only if the role may push."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.ctl.mcp_tools is None:
            raise RuntimeError("MCP server not built: no tool allowlist for the sandbox policy")
        dest.write_text(render_policy(self.cfg, agent=agent, push=push,
                                      mcp_tools=self.ctl.mcp_tools))
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
            reuse = run.role == "implementer" and any(
                r.sandbox == run.sandbox and r.id != run.id for r in self.runs.values())
            if reuse:
                await self.runner.upload(run.sandbox, payload)
            else:
                policy = self._policy(self._run_dir(run_id) / "policy.yaml",
                                      agent=True, push=run.role in PUSH_ROLES)
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
            self._event("sandbox", f"agent started in sandbox {run.sandbox}", run.node_id, run_id)
            self._save()
        rc = await proc.wait()
        async with self.lock:
            self.procs.pop(run_id, None)
            run.exit_code = rc
            if run.status in LIVE_RUN:
                self._fail_run(run, f"agent exited with code {rc} without an accepted report")
            else:
                self._event("agent", f"agent process exited ({rc})", run.node_id, run_id)
            self._archive_log(run)
            self._save()
