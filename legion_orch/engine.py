"""Workflow engine: the only authority over state transitions.

Agents act through MCP (documents, report) and git push; every claim is checked by guards
before the engine moves anything forward. See WORKFLOW.md for the specification.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shlex
import shutil
import sqlite3
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import attest, governance, guards, risk
from .assets import AssetError, Assets
from .config import Config
from .git import GitGrant, GitRemote
from .store import DocStore, DocVersion
from .util import now_iso
from .identity import JWT_SPIFFE, AccessTokens, DevSpire, IdentityService, SvidVerifier
from .model import (ACCEPTED, ACTIVE_NODE, AWAITING_HUMAN, BASELINE, BUILD, CRITIQUE, ESCALATED,
                    FAILED, H_CHANGE, H_REQUIREMENTS, H_SPEC, HUMAN, INTAKE, INTEGRATION, LIVE_RUN,
                    MERGING, ORACLE, PENDING, PLAN, R_ACCEPTED, R_ACTIVE, R_FAILED, R_REPORTED,
                    R_STARTING, R_SUPERSEDED, READY, RELEASE, REVIEWING, REWORK, ROLE_FOR_KIND,
                    RUNNING, SHIP, VERIFYING, Change, Node, Run, StateStore)
from .sandbox import (RUN_AGENT_SH, VERIFY_SETUP_SH, LocalRunner, Runner, SandboxError,
                      SandboxSpec, make_runner, render_policy)

log = logging.getLogger("legion")

PUSH_ROLES = {"implementer", "oracle"}
PANEL_ROLES = ("reviewer", "security", "architecture")
JUDGE_ROLES = {"critic", "integration", "baseline", *PANEL_ROLES}
PLAN_DOCS = ["plan/spec.md", "plan/oracle.json", "plan/taskgraph.json"]


class WorkflowError(Exception):
    """A refused human/API action (shown to the operator)."""


def _slug(node_id: str) -> str:
    return node_id.replace(":", "-").lower()


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


class Controller:
    """Shared resources and the scheduler loop. Holds one ChangeEngine per change.

    Durable state lives only in the document store (``state.db``) and the git remote; ``tmp/``
    holds transient sandbox payloads and live agent logs and is wiped on start.
    """

    def __init__(self, cfg: Config, runner: Runner | None = None):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.tmp = cfg.state_dir / "tmp"
        self.dblock = threading.RLock()
        self.conn = sqlite3.connect(cfg.state_dir / "state.db", check_same_thread=False)
        self.assets = Assets(cfg.assets_dir)
        self.docs = DocStore(self.conn, self.dblock, self.assets.schemas)
        self.git = GitRemote(cfg.state_dir / "git", cfg.repo.name, cfg.repo.path,
                             cfg.repo.base_branch)
        self.runner = runner or make_runner(cfg)
        self.lock = asyncio.Lock()
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.verifier_refs: dict[str, dict] = {}  # verifier sandbox ref -> {id, change, task}
        self.identity, self.dev_spire = self._identity()
        if isinstance(self.runner, LocalRunner):
            self.runner.gateway = self._dev_gateway
        self.tasks: set[asyncio.Task] = set()
        self.changes: dict[str, ChangeEngine] = {}
        self.mcp_tools: list[str] | None = None   # set by the MCP server (gateway allowlist)
        self.version = 0
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None

    def _identity(self) -> tuple[IdentityService, DevSpire | None]:
        ic = self.cfg.identity
        dev = DevSpire(ic.trust_prefix) if ic.dev_spire else None
        verifier = SvidVerifier(issuer=dev.issuer if dev else ic.issuer, audience=ic.svid_audience,
                                trust_prefix=ic.trust_prefix, jwks_uri=None if dev else ic.jwks_uri,
                                static_jwks=dev.jwks() if dev else None, ca_file=ic.jwks_ca_file)
        return IdentityService(verifier, AccessTokens(ic.token_audience, ic.access_ttl_s),
                               ic.scope, dev), dev

    async def _dev_gateway(self, sandbox_ref: str) -> str:
        """LocalRunner only: do what the OpenShell supervisor does – mint the sandbox's JWT-SVID
        (dev SPIRE) and exchange it at our own /oauth/token endpoint."""
        if self.dev_spire is None:
            raise RuntimeError("dev gateway requires identity.dev_spire")
        ic = self.cfg.identity
        form = {"grant_type": "client_credentials", "client_assertion_type": JWT_SPIFFE,
                "client_assertion": self.dev_spire.mint(sandbox_ref, ic.svid_audience),
                "audience": ic.token_audience, "scope": ic.scope}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{self.cfg.server.public_url.rstrip('/')}/oauth/token", data=form)
            r.raise_for_status()
            return r.json()["access_token"]

    @property
    def git_url(self) -> str:
        return f"{self.cfg.server.public_url.rstrip('/')}/git/{self.cfg.repo.name}.git"

    @property
    def mcp_url(self) -> str:
        return self.cfg.server.mcp_url

    def bump(self) -> None:
        self.version += 1
        self._wake.set()

    def spawn(self, coro, on_error=None) -> None:
        async def wrapper():
            try:
                await coro
            except Exception as e:  # never lose a background failure silently
                log.exception("background task failed")
                if on_error:
                    on_error(e)
        t = asyncio.create_task(wrapper())
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    # ------------------------------------------------------------------ lifecycle
    def load(self) -> None:
        """Rebuild every change from the store."""
        for m in self.docs.list("changes/"):
            parts = m["path"].split("/")
            if len(parts) == 4 and parts[2:] == ["control", "change.json"]:
                ce = ChangeEngine(self, parts[1])
                ce.st.load()
                self.changes[ce.cid] = ce

    async def start(self) -> None:
        await self.git.ensure()
        shutil.rmtree(self.tmp, ignore_errors=True)   # stale payloads may contain old tokens
        self.tmp.mkdir(parents=True)
        self.load()
        async with self.lock:
            for ce in self.changes.values():
                ce.recover()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
        for p in self.procs.values():
            if p.returncode is None:
                p.kill()

    async def _loop(self) -> None:
        while True:
            for ce in list(self.changes.values()):
                try:
                    await ce.tick()
                except Exception as e:
                    log.exception("tick failed")
                    ce._event("error", f"scheduler error: {e}")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ changes
    async def start_change(self, goal: str) -> "ChangeEngine":
        goal = goal.strip()
        if not goal:
            raise WorkflowError("goal is required")
        async with self.lock:
            base = await self.git.resolve_base()
            while True:
                cid = "c" + datetime.now(timezone.utc).strftime("%y%m%d") + "-" + secrets.token_hex(2)
                if cid not in self.changes:
                    break
            await self.git.init_integration(cid, base)
            ce = ChangeEngine(self, cid)
            ce.st.change = Change(id=cid, goal=goal, base_sha=base, integration_head=base)
            self.changes[cid] = ce
            ce._cwrite("control/goal.md", f"# Goal\n\n{goal}\n\nBase: `{base}`\n")
            ce._add(Node(INTAKE, INTAKE, "Intake: requirements & risk"))
            ce._add(Node(H_REQUIREMENTS, HUMAN, "Accept requirements & tier", depends_on=[INTAKE]))
            ce._event("change", f"change {cid} started at {base[:12]}", data={"goal": goal})
            ce._save()
            return ce

    def get(self, cid: str) -> "ChangeEngine":
        try:
            return self.changes[cid]
        except KeyError:
            raise WorkflowError(f"unknown change {cid}") from None

    def list_changes(self) -> list[dict]:
        out = []
        for ce in self.changes.values():
            c = ce.change
            out.append({"id": c.id, "goal": c.goal, "status": c.status, "tier": c.tier,
                        "created_at": c.created_at,
                        "accepted": sum(n.status == ACCEPTED for n in ce.nodes.values()),
                        "nodes": len(ce.nodes),
                        "live_runs": sum(r.status in LIVE_RUN for r in ce.runs.values())})
        return sorted(out, key=lambda c: c["created_at"], reverse=True)

    # ------------------------------------------------------------------ runs / tokens
    def all_runs(self):
        for ce in self.changes.values():
            for r in ce.runs.values():
                yield ce, r

    def live_count(self) -> int:
        return sum(1 for _, r in self.all_runs() if r.status in LIVE_RUN)

    def next_run_id(self) -> str:
        n = max((int(r.id[1:]) for _, r in self.all_runs()), default=0)
        return f"r{n + 1:04d}"

    def find_run(self, run_id: str) -> tuple["ChangeEngine", Run]:
        for ce, r in self.all_runs():
            if r.id == run_id:
                return ce, r
        raise WorkflowError(f"unknown run {run_id}")

    def sandbox_binding(self, ref: str) -> dict[str, str] | None:
        """The one task (and role) a live, managed sandbox serves – None if unknown or deleted.
        A sandbox serves exactly one task for its whole life (an implementer sandbox is reused
        across that task's rework cycles; judges and verifiers always get their own)."""
        if ref in self.verifier_refs:
            v = self.verifier_refs[ref]
            return {"sandbox": ref, "change": v["change"], "task": v["task"], "role": "verifier"}
        for ce, r in self.all_runs():
            if r.sandbox_ref == ref and not r.sandbox_deleted:
                return {"sandbox": ref, "change": ce.cid, "task": r.node_id, "role": r.role}
        return None

    def run_for_bearer(self, token: str | None) -> tuple["ChangeEngine", Run] | None:
        """Bind a gateway-injected, task-scoped token to the sandbox's live run, per request.
        The run must match the token's change, task and role exactly."""
        claims = self.identity.tokens.verify(token) if token else None
        if not claims:
            return None
        for ce, r in self.all_runs():
            if r.sandbox_ref == claims["sandbox"] and r.status in LIVE_RUN:
                if (ce.cid, r.node_id, r.role) == (claims["change"], claims["task"], claims["role"]):
                    return ce, r
                log.warning("token for %s does not match live run %s", claims, r.id)
                return None
        return None

    def git_grant(self, token: str) -> GitGrant | None:
        claims = self.identity.tokens.verify(token) if token else None
        if not claims:
            return None
        if claims["role"] == "verifier":
            v = self.verifier_refs.get(claims["sandbox"])
            if v and (v["change"], v["task"]) == (claims["change"], claims["task"]):
                return GitGrant(v["id"], None)          # read-only
            return None
        found = self.run_for_bearer(token)
        if found is None:
            return None
        run = found[1]
        return GitGrant(run.id, f"refs/heads/{run.branch}" if run.role in PUSH_ROLES else None)


class ChangeEngine:
    """Workflow state machine for one change; all durable state goes to the store."""

    def __init__(self, ctl: Controller, cid: str):
        self.ctl = ctl
        self.cid = cid
        self.cfg, self.docs, self.git = ctl.cfg, ctl.docs, ctl.git
        self.assets, self.runner, self.lock = ctl.assets, ctl.runner, ctl.lock
        self.procs = ctl.procs
        self.st = StateStore(ctl.docs, cid)

    # ================================================================== basics
    @property
    def change(self) -> Change | None:
        return self.st.change

    @property
    def nodes(self) -> dict[str, Node]:
        return self.st.nodes

    @property
    def runs(self) -> dict[str, Run]:
        return self.st.runs

    @property
    def git_url(self) -> str:
        return self.ctl.git_url

    @property
    def mcp_url(self) -> str:
        return self.ctl.mcp_url

    def p(self, rel: str) -> str:
        """Qualify a document path into this change's namespace."""
        return attest.qualify(rel, self.cid)

    def _save(self) -> None:
        self.st.save()
        self.ctl.bump()

    def _event(self, kind: str, message: str, node: str | None = None, run: str | None = None,
               data: Any = None) -> None:
        self.st.event(kind, message, node, run, data)
        log.info("[%s %s] %s%s", self.cid, kind, f"{node}: " if node else "", message)
        self.ctl.version += 1

    def _spawn(self, coro) -> None:
        self.ctl.spawn(coro, on_error=lambda e: self._event(
            "error", f"internal error: {e}", data=traceback.format_exc()[-3000:]))

    def _cwrite(self, rel: str, content: str | dict) -> DocVersion:
        path = self.p(rel)
        if not isinstance(content, str):
            content = json.dumps(content, indent=2, sort_keys=True)
        return self.docs.write(path, content, author="controller", role="controller",
                               base_version=self.docs.latest_version(path))

    def _latest(self, rel: str) -> int | None:
        return self.docs.latest_version(self.p(rel))

    def _doc(self, rel: str, version: int | None = None) -> DocVersion:
        return self.docs.read(self.p(rel), version)

    def _latest_att(self, rel: str) -> str | None:
        v = self._latest(rel)
        return f"file:{self.p(rel)}@{v}" if v else None

    def _json_doc(self, rel: str, version: int | None = None) -> Any:
        return json.loads(self._doc(rel, version).content)

    def recover(self) -> None:
        """After a restart the agent processes are gone, so live runs fail (and escalate);
        interrupted verification, merge and ship steps resume."""
        for run in self.runs.values():
            if run.status in LIVE_RUN:
                self._fail_run(run, "controller restarted while the run was live")
        for node in self.nodes.values():
            if node.status == VERIFYING:
                self._spawn(self._verify_node(node.id))
            elif node.status == MERGING:
                self._spawn(self._merge_node(node.id))
            elif node.kind == SHIP and node.status == RUNNING:
                self._spawn(self._ship())
        self._save()

    def _add(self, node: Node) -> None:
        self.nodes[node.id] = node

    def _create_stage_nodes(self) -> None:
        tier = self.change.tier
        r = risk.rank(tier)
        self._add(Node(PLAN, PLAN, "Plan: spec, oracle, task graph", depends_on=[H_REQUIREMENTS]))
        self._add(Node(CRITIQUE, CRITIQUE, "Critique: pre-mortem", depends_on=[PLAN]))
        h2_deps = [CRITIQUE]
        if r >= 2:
            self._add(Node(ORACLE, ORACLE, "Oracle: protected acceptance tests",
                           depends_on=[CRITIQUE]))
            h2_deps = [ORACLE]
        if r >= 3:
            self._add(Node(BASELINE, BASELINE, "Independent baseline", depends_on=[ORACLE]))
            h2_deps = [BASELINE]
        self._add(Node(H_SPEC, HUMAN, "Approve spec & oracle", depends_on=h2_deps))
        if r >= 2:
            self._add(Node(INTEGRATION, INTEGRATION, "Integration verification",
                           depends_on=[H_SPEC]))  # replaced by build ids at critique PASS
        if r >= 3:
            self._add(Node(RELEASE, RELEASE, "Release & rollback plan", depends_on=[INTEGRATION]))
        if r >= 2:
            self._add(Node(H_CHANGE, HUMAN, "Accept whole change",
                           depends_on=[RELEASE if r >= 3 else INTEGRATION]))
        self._add(Node(SHIP, SHIP, "Ship readiness check",
                       depends_on=[H_CHANGE] if r >= 2 else [H_SPEC]))

    def _build_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == BUILD]

    def _materialize_builds(self, taskgraph: dict) -> None:
        for n in self._build_nodes():
            del self.nodes[n.id]
        wv = guards.waves(taskgraph["tasks"])
        ids = []
        for t in taskgraph["tasks"]:
            nid = f"build:{t['id']}"
            ids.append(nid)
            self._add(Node(nid, BUILD, f"{t['id']}: {t['title']}", task=t, wave=wv[t["id"]],
                           depends_on=[H_SPEC] + [f"build:{d}" for d in t["depends_on"]]))
        # downstream of builds
        if INTEGRATION in self.nodes:
            self.nodes[INTEGRATION].depends_on = list(ids)
        else:
            self.nodes[SHIP].depends_on = list(ids)
        # keep the stage order stable for display: move integration+ after builds
        for nid in (INTEGRATION, RELEASE, H_CHANGE, SHIP):
            if nid in self.nodes:
                self.nodes[nid] = self.nodes.pop(nid)

    # ================================================================== scheduler
    def _deps_accepted(self, node: Node) -> bool:
        """Guard G-D1."""
        return all(self.nodes.get(d) is not None and self.nodes[d].status == ACCEPTED
                   for d in node.depends_on)

    def _live_runs(self) -> list[Run]:
        return [r for r in self.runs.values() if r.status in LIVE_RUN]

    def _overlaps_running(self, node: Node) -> bool:
        """Guard G-D3 (file overlap part)."""
        from .git import paths_overlap
        mine = node.task["files_modified"]
        for other in self._build_nodes():
            if other.id == node.id or other.status not in ACTIVE_NODE | {REWORK}:
                continue
            if any(paths_overlap(a, b) for a in mine for b in other.task["files_modified"]):
                return True
        return False

    async def tick(self) -> None:
        launches: list[Run] = []
        async with self.lock:
            if self.change is None or self.change.status != "ACTIVE":
                return
            for node in list(self.nodes.values()):
                if node.status == PENDING and self._deps_accepted(node):
                    if node.kind == HUMAN:
                        node.status = AWAITING_HUMAN
                        self._event("human", "awaiting human decision", node.id)
                    elif node.kind == SHIP:
                        node.status = RUNNING
                        self._spawn(self._ship())
                    else:
                        node.status = READY
            self._check_timeouts()
            capacity = self.cfg.limits.max_parallel - self.ctl.live_count()
            for node in list(self.nodes.values()):
                if capacity <= 0:
                    break
                if node.status in (READY, REWORK) and node.kind in ROLE_FOR_KIND:
                    if not self._deps_accepted(node):
                        continue
                    if node.kind == BUILD and self._overlaps_running(node):
                        continue
                    run = self._prepare(node, ROLE_FOR_KIND[node.kind])
                    if run:
                        launches.append(run)
                        capacity -= 1
                elif node.status == REVIEWING:
                    for role in self.change.panel:
                        if capacity <= 0:
                            break
                        key = f"{node.cycle}:{role}"
                        busy = any(r.node_id == node.id and r.role == role and r.cycle == node.cycle
                                   and r.status in LIVE_RUN | {R_REPORTED} for r in self.runs.values())
                        if key not in node.reviews and not busy:
                            run = self._prepare(node, role, judged_sha=node.candidate_sha)
                            if run:
                                launches.append(run)
                                capacity -= 1
            self._save()
        for run in launches:
            self._spawn(self._launch(run.id))

    def _check_timeouts(self) -> None:
        limit = self.cfg.limits.run_timeout_s
        now = datetime.now(timezone.utc)
        for run in self._live_runs():
            if (now - _parse_ts(run.created_at)).total_seconds() > limit:
                proc = self.procs.get(run.id)
                if proc and proc.returncode is None:
                    proc.kill()
                self._fail_run(run, f"timed out after {limit}s")

    # ================================================================== dispatch
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
                refs += [a for a in (self._latest_att(p) for p in self._plan_docs()) if a]
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
                ev = self._latest_att(f"tasks/{node.task['id']}/evidence/cycle-{node.cycle}.md")
                if ev:
                    refs.append(ev)
                ver = node.verification.get(str(node.cycle))
                if ver and ver.get("attestation"):
                    refs.append(ver["attestation"])
        if role in ("integration", "release"):
            for b in self._build_nodes():
                refs += [a for a in (self._latest_att(f"tasks/{b.task['id']}/evidence/cycle-{b.cycle}.md"),)
                         if a]
            if role == "release":
                a = self._latest_att("integration/report.md")
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

    def _policy(self, template: str, dest: Path, *, agent: bool, push: bool) -> Path:
        """Sandbox policy = static template + generated controller endpoints (policy.py):
        MCP vetted by the gateway (protocol: mcp, tool allowlist), git push only if allowed."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.ctl.mcp_tools is None:
            raise RuntimeError("MCP server not built: no tool allowlist for the sandbox policy")
        dest.write_text(render_policy(self.cfg, template, agent=agent, push=push,
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
                policy = self._policy(self.cfg.sandbox.policy_template,
                                      self._run_dir(run_id) / "policy.yaml",
                                      agent=True, push=run.role in PUSH_ROLES)
                run.sandbox_ref = await self.runner.create(SandboxSpec(
                    name=run.sandbox, payload_dir=payload, policy_file=policy, agent=True,
                    labels={"legion.change": self.change.id, "legion.run": run.id,
                            "legion.role": run.role}))
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

    # ================================================================== failure / escalation
    def _escalate(self, node: Node, kind: str, reason: str, resume: str, **extra) -> None:
        node.status = ESCALATED
        node.escalation = {"type": kind, "reason": reason, "resume": resume, "at": now_iso(),
                           **extra}
        self._event("escalation", f"{kind}: {reason}", node.id, extra.get("run"))

    def _fail_run(self, run: Run, reason: str) -> None:
        """No automatic retries (principle 5): a failed run escalates to a human."""
        run.status = R_FAILED
        run.ended_at = now_iso()
        node = self.nodes.get(run.node_id)
        self._event("run_failed", reason, run.node_id, run.id)
        if node is None:
            return
        tail = self.log_tail(run.id, 40)
        if run.role in PANEL_ROLES:
            self._escalate(node, "run_failed", f"{run.role} run {run.id}: {reason}",
                           resume=REVIEWING, run=run.id, log_tail=tail)
        else:
            resume = REWORK if node.kind == BUILD else READY
            self._escalate(node, "run_failed", f"run {run.id}: {reason}", resume=resume,
                           run=run.id, log_tail=tail)
        if not self.cfg.sandbox.keep_failed and run.sandbox and run.role != "implementer":
            self._spawn(self._delete_sandbox(run.sandbox))

    LOG_LIMIT = 512 * 1024

    def _archive_log(self, run: Run) -> None:
        """Move the agent log into the store (agents cannot read control/logs/)."""
        f = self._run_dir(run.id) / "agent.log"
        if not f.exists():
            return
        data = f.read_bytes()
        text = data[-self.LOG_LIMIT:].decode(errors="replace")
        if len(data) > self.LOG_LIMIT:
            text = f"[truncated: last {self.LOG_LIMIT} of {len(data)} bytes]\n" + text
        self._cwrite(f"control/logs/{run.id}.log", text)
        shutil.rmtree(self._run_dir(run.id), ignore_errors=True)

    def log_tail(self, run_id: str, lines: int = 200) -> str:
        p = self._run_dir(run_id) / "agent.log"
        if p.exists():
            text = p.read_bytes()[-200_000:].decode(errors="replace")
        elif self._latest(f"control/logs/{run_id}.log"):
            text = self._doc(f"control/logs/{run_id}.log").content
        else:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    async def _delete_sandbox(self, name: str) -> None:
        try:
            await self.runner.delete(name)
        except Exception as e:
            self._event("sandbox", f"delete of {name} failed: {e}")
            return
        async with self.lock:
            for r in self.runs.values():
                if r.sandbox == name:
                    r.sandbox_deleted = True
            self._event("sandbox", f"sandbox {name} deleted")
            self._save()

    def _accept_runs(self, node: Node, roles: set[str] | None = None) -> None:
        """Mark the node's reported runs ACCEPTED and delete their sandboxes (principle 8)."""
        sandboxes = set()
        for rid in node.runs:
            r = self.runs[rid]
            if roles and r.role not in roles:
                continue
            if r.status == R_REPORTED:
                r.status = R_ACCEPTED
            if r.sandbox and not r.sandbox_deleted and r.status in (R_ACCEPTED, R_SUPERSEDED, R_FAILED):
                sandboxes.add(r.sandbox)
        for sb in sandboxes:
            if any(r.sandbox == sb and r.status in LIVE_RUN for r in self.runs.values()):
                continue
            self._spawn(self._delete_sandbox(sb))

    # ================================================================== MCP-facing
    def docs_write(self, run: Run, path: str, content: str, base_version: int | None) -> DocVersion:
        """Guards G-W1 and G-W2. ``path`` may be relative to the change namespace."""
        path = self.p(path)
        rel = path[len(self.p("")):]
        controller_owned = rel.startswith("control/") or (
            rel.startswith("tasks/") and (rel.split("/")[2:3] in (["verification.json"], ["feedback"],
                                                                  ["decisions"])))
        if controller_owned or not any(path.startswith(w) for w in run.write_scope):
            raise WorkflowError(f"G-W1: {path} is outside your write scope {run.write_scope}")
        v = self.docs.write(path, content, author=run.id, role=run.role, base_version=base_version)
        self._event("doc", f"{v.attestation} written", run.node_id, run.id)
        return v

    def progress(self, run: Run, message: str, percent: float | None) -> None:
        run.progress.append({"at": now_iso(), "message": message[:500], "percent": percent})
        run.progress = run.progress[-50:]
        if run.status == R_STARTING:
            run.status = R_ACTIVE
        self.ctl.version += 1

    async def handle_report(self, run_id: str, status: str, summary: str, attestations: list[str],
                            data: dict | None, blocked_reason: str | None,
                            missing_decision: str | None) -> dict:
        async with self.lock:
            run = self.runs[run_id]
            if run.status not in LIVE_RUN:
                return {"accepted": False, "violations": [f"run is {run.status}"]}
            node = self.nodes[run.node_id]
            run.attempts += 1
            data = data or {}
            attestations = [attest.normalize(a, self.cid) for a in attestations]
            for f in data.get("findings") or []:
                if isinstance(f, dict) and isinstance(f.get("attestations"), list):
                    f["attestations"] = [attest.normalize(a, self.cid) for a in f["attestations"]
                                         if isinstance(a, str)]
            v = guards.report_shape(status, blocked_reason, missing_decision)
            if status == "DONE":   # G-R7: structured data matches the role's report schema
                schema = self.assets.roles[run.role].report_schema
                v += [f"G-R7: data does not match schema {schema!r}: {x}"
                      for x in self.assets.schemas.validate_data(schema, data)]
            parsed, v3 = await attest.check_all(attestations, [self.cfg.repo.name], self.docs, self.git)
            v += v3
            ctx: dict[str, Any] = {}
            if not v and status == "DONE":
                v += await self._role_guards(run, node, parsed, data, ctx)
            if v:
                run.violations.append(v)
                left = self.cfg.limits.max_report_attempts - run.attempts
                self._event("report_rejected", f"{len(v)} violation(s): " + "; ".join(v)[:600],
                            node.id, run.id, data=v)
                if left <= 0:
                    self._fail_run(run, "G-R6: too many rejected reports")
                self._save()
                return {"accepted": False, "violations": v, "attempts_left": max(left, 0)}
            run.status = R_REPORTED
            run.ended_at = now_iso()
            run.report = {"status": status, "summary": summary, "attestations": attestations,
                          "data": data, "blocked_reason": blocked_reason,
                          "missing_decision": missing_decision, "at": run.ended_at}
            self._event("report", f"{run.role} reported {status}: {summary[:200]}", node.id, run.id)
            if status == "BLOCKED":
                self._escalate(node, "blocked", blocked_reason or "",
                               resume=REWORK if node.kind == BUILD else READY,
                               run=run.id, missing_decision=missing_decision)
            else:
                await self._on_done(run, node, data, ctx)
            self._save()
            return {"accepted": True, "next": node.status}

    # ------------------------------------------------------------------ role guards
    async def _role_guards(self, run: Run, node: Node, parsed: list[attest.Attestation],
                           data: dict, ctx: dict) -> list[str]:
        role = self.assets.roles[run.role]
        found, v = guards.required_docs(run.id, run.required_docs, parsed, self.docs)
        sha, vg = guards.required_git(role.git, parsed, self.cfg.repo.name, run.judged_sha)
        v += vg
        if v:
            return v
        ctx["docs"], ctx["sha"] = found, sha
        repo_atts = {str(a) for a in parsed}
        if role.git == "commit":
            tip = await self.git.ref_sha(f"refs/heads/{run.branch}")
            if tip != sha:
                return [f"G-B2: {sha} is not the tip of {run.branch} on the controller remote "
                        f"(tip: {tip or 'branch not pushed'}) – push before reporting"]
            if not await self.git.is_ancestor(run.base_sha, sha):
                return [f"G-B2: {sha} does not descend from base {run.base_sha}"]
        if role.git == "head" and sha != self.change.integration_head:
            return [f"G-R5: integration head moved to {self.change.integration_head}"]

        if run.role == "intake":
            req = self.docs.read(self.p("intake/requirements.md"), found[self.p("intake/requirements.md")].version)
            rj, e = guards.load_json(self.docs, found[self.p("intake/risk.json")], "G-I2")
            if e:
                return e
            q = self._doc("intake/questions.md").content if self._latest("intake/questions.md") else None
            v += guards.intake(req.content, rj, q, "DONE")
        elif run.role == "planner":
            tg, e1 = guards.load_json(self.docs, found[self.p("plan/taskgraph.json")], "G-P1")
            orc, e2 = guards.load_json(self.docs, found[self.p("plan/oracle.json")], "G-P1")
            if e1 or e2:
                return e1 + e2
            v += guards.plan(tg, orc, self.change.requirements, self.change.tier,
                             self.cfg.protected_paths, self.assets.roles["implementer"].souls_allowed)
            _, vs = guards.required_docs(run.id, [self.p(f"plan/specs/{r}.md")
                                                  for r in self.change.requirements], parsed, self.docs)
            v += [x.replace("G-R4", "G-P9") for x in vs]
            ctx["taskgraph"], ctx["oracle"] = tg, orc
        elif run.role == "critic":
            plan_atts = {a.path: a.version for a in parsed if a.kind == "file"}
            latest = {self.p(p): self._latest(p) for p in PLAN_DOCS}
            v += guards.critique(data, plan_atts, latest, repo_atts)
        elif run.role == "oracle":
            orc = self._json_doc("plan/oracle.json")
            changed = await self.git.changed_paths(run.base_sha, sha)
            v += guards.scoped_diff(changed, orc.get("acceptance_tests", []), [],
                                    self.cfg.protected_paths, "G-O1")
            for p in orc.get("acceptance_tests", []):
                if not await self.git.has_object(sha, p):
                    v.append(f"G-O2: acceptance test {p} missing at {sha[:12]}")
        elif run.role == "baseline":
            orc = self._json_doc("plan/oracle.json")
            v += guards.results_cover(data, orc.get("acceptance_commands", [])
                                      + orc.get("integration_commands", []), "G-BL1")
        elif run.role == "implementer":
            t = node.task
            changed = await self.git.changed_paths(run.base_sha, sha)
            approved = governance.approved_protected_for(self, t["id"], run.created_at)  # G-PP1
            protected = [p for p in governance.protected_paths(self) if p not in approved]
            v += guards.scoped_diff(changed, t["files_modified"] + approved,
                                    t.get("files_forbidden", []), protected, "G-B3")
            ev = self.docs.read(run.required_docs[0], found[run.required_docs[0]].version).content
            v += guards.evidence_mentions(ev, t["verification"])
        elif run.role in PANEL_ROLES:
            v += guards.findings(data, repo_atts)
            v += guards.verdict(data, pass_word="PASS", fail_word="FAIL", gid="G-RV3",
                                cycle=run.cycle, require_checked=True)
        elif run.role == "integration":
            orc = self.change.oracle or {}
            v += guards.verdict(data, pass_word="PASS", fail_word="FAIL", gid="G-INT1",
                                cycle=2, require_checked=False)
            v += [x.replace("G-RV2", "G-INT1") for x in guards.findings(data, repo_atts)]
            v += guards.results_cover(data, self._oracle_commands(), "G-INT1")
            v += await self._protected_unchanged(sha)
        elif run.role == "release":
            v += guards.release_docs(self._doc("release/plan.md").content,
                                     self._doc("release/rollback.md").content,
                                     self.change.base_sha)
        return v

    async def _protected_unchanged(self, head: str) -> list[str]:
        """Guard G-INT2."""
        v = []
        if not self.change.oracle_sha:
            return v
        approved = governance.all_approved_protected(self)
        for p in (self.change.oracle or {}).get("acceptance_tests", []):
            if p in approved:
                continue
            if await self.git.blob(head, p) != await self.git.blob(self.change.oracle_sha, p):
                v.append(f"G-INT2: protected acceptance test {p} changed since oracle commit")
        return v

    # ------------------------------------------------------------------ transitions
    async def _on_done(self, run: Run, node: Node, data: dict, ctx: dict) -> None:
        ch = self.change
        role = run.role
        if role == "intake":
            node.status = ACCEPTED
            rj = self._json_doc("intake/risk.json", ctx["docs"][self.p("intake/risk.json")].version)
            ch.risk = risk.derive_tier(rj)
            self._event("risk", f"derived tier {ch.risk['tier']} (score {ch.risk['total']})", node.id)
            self._accept_runs(node)
        elif role == "planner":
            node.status = ACCEPTED
            self._accept_runs(node)
            crit = self.nodes[CRITIQUE]
            crit.status = PENDING
        elif role == "critic":
            self._accept_runs(node)
            if data["verdict"] == "PASS":
                node.status = ACCEPTED
                self._materialize_builds(self._json_doc("plan/taskgraph.json"))
                self._event("plan", f"plan accepted by critic; {len(self._build_nodes())} build task(s)")
            else:
                plan = self.nodes[PLAN]
                limit = self.cfg.limits.critique_max_cycles + node.extra_cycles
                fb = {"cycle": plan.cycle, "source": "critique", "text": "critique requested rework: "
                      + "; ".join(f"{f.get('severity')}: {f.get('title')}" for f in data.get("findings", [])),
                      "attestations": [str(a) for a in ctx["docs"].values()]}
                if plan.cycle >= limit:
                    self._escalate(node, "critique_budget",
                                   f"G-C2: {plan.cycle} critique cycles without PASS", resume="critique_budget")
                    node.feedback.append(fb)
                else:
                    plan.cycle += 1
                    plan.feedback.append(fb)
                    plan.status = REWORK
                    node.cycle += 1
                    node.status = PENDING
        elif role == "oracle":
            node.candidate_sha = ctx["sha"]
            await self.git.pin(run.id, ctx["sha"])
            ok, head = await self.git.merge(ch.id, ctx["sha"], f"legion {ch.id}: oracle (protected acceptance tests)")
            if not ok:
                self._escalate(node, "merge", f"oracle merge failed: {head[:400]}", resume=READY)
                return
            ch.oracle_sha = ctx["sha"]
            ch.integration_head = head
            ch.oracle = self._json_doc("plan/oracle.json")
            node.accepted_sha = ctx["sha"]
            node.status = ACCEPTED
            self._accept_runs(node)
        elif role == "baseline":
            node.status = ACCEPTED
            self._accept_runs(node)
        elif role == "implementer":
            node.candidate_sha = ctx["sha"]
            await self.git.pin(run.id, ctx["sha"])
            node.status = VERIFYING
            self._spawn(self._verify_node(node.id))
        elif role in PANEL_ROLES:
            node.reviews[f"{run.cycle}:{role}"] = {
                "verdict": data["verdict"], "sha": run.judged_sha, "run": run.id,
                "findings": data.get("findings", []), "checked": data.get("checked", []),
                "doc": str(ctx["docs"][run.required_docs[0]])}
            run.status = R_ACCEPTED
            self._spawn(self._delete_sandbox(run.sandbox))
            self._panel_complete(node)
        elif role == "integration":
            node.reviews["integration"] = {"verdict": data["verdict"], "sha": ctx["sha"],
                                           "findings": data.get("findings", []), "run": run.id}
            if data["verdict"] != "PASS":
                self._escalate(node, "integration_failed", "integration verifier reported FAIL",
                               resume=READY, run=run.id)
                return
            node.candidate_sha = ctx["sha"]
            node.status = VERIFYING
            self._spawn(self._verify_node(node.id))
        elif role == "release":
            node.status = ACCEPTED
            self._accept_runs(node)

    def _panel_complete(self, node: Node) -> None:
        results = [node.reviews.get(f"{node.cycle}:{r}") for r in self.change.panel]
        if any(r is None for r in results):
            return
        if all(r["verdict"] == "PASS" and r["sha"] == node.candidate_sha for r in results):
            node.status = MERGING
            self._spawn(self._merge_node(node.id))
            return
        high = [f for r in results for f in guards.high_findings(r)]
        fps = sorted({guards.fingerprint(f) for f in high})
        node.finding_history.append(fps)
        docs = [r["doc"] for r in results]
        fb = [{"cycle": node.cycle, "source": "review",
               "text": f"{f['severity']} ({f.get('dimension', '?')}): {f['title']} – fix: {f['fix']}",
               "attestations": f.get("attestations", [])} for f in high]
        fb.append({"cycle": node.cycle, "source": "review", "text": "full review documents",
                   "attestations": docs})
        if len(node.finding_history) >= 2 and fps and node.finding_history[-1] == node.finding_history[-2]:
            node.feedback += fb
            self._escalate(node, "stale_loop", "G-RV5: identical HIGH findings in two consecutive "
                           "review cycles", resume="rework_cycle")
            return
        self._rework(node, fb, "review")

    def _rework(self, node: Node, feedback: list[dict], source: str) -> None:
        """Evidence-driven rework within the cycle budget (G-RV5)."""
        node.feedback += feedback
        limit = self.cfg.limits.review_max_cycles + node.extra_cycles
        cur = self.runs.get(node.current_run) if node.current_run else None
        if cur and cur.status == R_REPORTED:
            cur.status = R_SUPERSEDED
        if node.cycle >= limit:
            self._escalate(node, "cycle_budget",
                           f"G-RV5: {node.cycle} cycle(s) used without acceptance ({source})",
                           resume="rework_cycle")
            return
        node.cycle += 1
        node.candidate_sha = None
        node.status = REWORK
        self._event("rework", f"{source}: rework cycle {node.cycle}", node.id)

    # ================================================================== deterministic verification
    async def _verify(self, label: str, sha: str, commands: list[str]) -> dict:
        """G-V1: run commands at ``sha`` in a fresh sandbox. Never raises."""
        name = f"lg-{self.change.id}-v{secrets.token_hex(3)}".lower()
        base = self.ctl.tmp / "verify" / name
        payload = base / "payload"
        payload.mkdir(parents=True, exist_ok=True)
        (payload / "run.env").write_text(
            f"LEGION_GIT_URL={shlex.quote(self.git_url)}\n")
        (payload / "verify-setup.sh").write_text(VERIFY_SETUP_SH)
        result: dict[str, Any] = {"sha": sha, "sandbox": name, "results": [], "ok": False,
                                  "infra_error": None, "at": now_iso()}
        created, ref = False, None
        try:
            policy = self._policy(self.cfg.sandbox.verifier_policy_template, base / "policy.yaml",
                                  agent=False, push=False)
            ref = await self.runner.create(SandboxSpec(
                name=name, payload_dir=payload, policy_file=policy, agent=False,
                labels={"legion.change": self.change.id, "legion.role": "verifier"}))
            self.ctl.verifier_refs[ref] = {"id": f"verifier:{name}", "change": self.cid,
                                           "task": label}         # read-only git grant
            created = True
            rc, out = await self.runner.exec(name, ["bash", "/sandbox/payload/verify-setup.sh", sha],
                                             timeout=600)
            if rc != 0:
                raise SandboxError(f"verifier setup failed ({rc}): {out[-1500:]}")
            ok = True
            for cmd in commands:
                rc, out = await self.runner.exec(name, ["bash", "-lc", cmd], workdir="/sandbox/work",
                                                 timeout=self.cfg.limits.verify_timeout_s)
                result["results"].append({"command": cmd, "exit_code": rc, "output": out[-4000:]})
                ok = ok and rc == 0
            result["ok"] = ok and bool(commands)
            if not commands:
                result["infra_error"] = "no commands to verify"
        except Exception as e:
            result["infra_error"] = str(e)
        finally:
            if ref:
                self.ctl.verifier_refs.pop(ref, None)
            shutil.rmtree(base, ignore_errors=True)
            if created:
                try:
                    await self.runner.delete(name)
                except Exception as e:
                    self._event("sandbox", f"verifier delete failed: {e}")
        return result

    async def _verify_node(self, node_id: str) -> None:
        node = self.nodes[node_id]
        if node.kind == BUILD:
            commands = node.task["verification"]
        else:
            commands = self._oracle_commands()
        self._event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                    f"({len(commands)} command(s))", node_id)
        res = await self._verify(node_id, node.candidate_sha, commands)
        async with self.lock:
            if node.status != VERIFYING:
                return
            att = self._cwrite(f"tasks/{node.task['id']}/verification.json" if node.kind == BUILD
                              else f"control/verification/{_slug(node_id)}.json", res).attestation
            res["attestation"] = att
            key = "integration" if node.kind == INTEGRATION else str(node.cycle)
            node.verification[key] = {k: res[k] for k in ("ok", "sha", "attestation", "infra_error")} \
                | {"results": [{"command": r["command"], "exit_code": r["exit_code"]} for r in res["results"]]}
            if res["infra_error"]:
                self._escalate(node, "verify_infra", res["infra_error"], resume=VERIFYING)
            elif not res["ok"]:
                fails = [r for r in res["results"] if r["exit_code"] != 0]
                if node.kind == BUILD:
                    self._rework(node, [{"cycle": node.cycle, "source": "verification",
                                         "text": f"BLOCKER: `{r['command']}` exited {r['exit_code']}: "
                                                 f"{r['output'][-800:]}", "attestations": [att]}
                                        for r in fails], "verification")
                else:
                    self._escalate(node, "integration_failed",
                                   f"G-V1: {len(fails)} oracle command(s) failed at head", resume=READY)
            elif node.kind == BUILD:
                if self.change.panel:
                    node.status = REVIEWING
                    self._event("review", f"verification passed; panel {self.change.panel}", node_id)
                else:
                    node.status = MERGING
                    self._spawn(self._merge_node(node_id))
            else:  # integration
                node.status = ACCEPTED
                self._accept_runs(node)
            self._save()

    # ================================================================== accept & merge
    async def _merge_node(self, node_id: str) -> None:
        async with self.lock:
            node = self.nodes[node_id]
            sha = node.candidate_sha
            ver = node.verification.get(str(node.cycle))
            override = (node.human or {}).get("override")
            v = []
            if not ver or not ver["ok"] or ver["sha"] != sha:
                v.append("G-A1: no passing deterministic verification at candidate")
            if not override:
                for role in self.change.panel:
                    r = node.reviews.get(f"{node.cycle}:{role}")
                    if not r or r["verdict"] != "PASS" or r["sha"] != sha:
                        v.append(f"G-A1: no {role} PASS at candidate")
            if v:
                self._escalate(node, "accept_guard", "; ".join(v), resume=REWORK)
                self._save()
                return
            ok, out = await self.git.merge(self.cid, sha, f"legion {self.change.id}: {node.title} "
                                                f"(cycle {node.cycle})")
            if not ok:
                head = await self.git.integration_head(self.change.id)
                self._rework(node, [{"cycle": node.cycle, "source": "merge",
                                     "text": f"G-A2: merge conflict with integration; rebase onto {head} "
                                             f"and force-push your branch. Details: {out[-600:]}",
                                     "attestations": [f"git:{self.cfg.repo.name}@{head}"]}], "merge")
                if node.status == REWORK:
                    node.base_sha = head
                self._save()
                return
            self.change.integration_head = out
            node.accepted_sha = sha
            node.status = ACCEPTED
            self._event("accepted", f"merged {sha[:12]} -> integration {out[:12]}", node_id)
            self._accept_runs(node)  # deletes the implementer sandbox (principle 8)
            self._save()

    # ================================================================== human actions
    def _check_approver(self, approver: str) -> None:
        if approver not in self.cfg.decision_owners:
            raise WorkflowError(f"approver {approver!r} is not a decision owner "
                                f"({', '.join(self.cfg.decision_owners)})")

    async def human_action(self, node_id: str, action: str, approver: str,
                           payload: dict | None = None) -> dict:
        payload = payload or {}
        async with self.lock:
            self._check_approver(approver)
            node = self.nodes.get(node_id)
            if node is None:
                raise WorkflowError(f"unknown node {node_id}")
            if node.status == ESCALATED:
                res = self._resolve_escalation(node, action, approver, payload)
            elif node.kind == HUMAN and node.status == AWAITING_HUMAN:
                res = await self._human_gate(node, action, approver, payload)
            elif node.kind == SHIP and action == "recheck" and node.status == ACCEPTED:
                node.status = RUNNING
                self._spawn(self._ship())
                res = {"ok": True}
            else:
                raise WorkflowError(f"action {action!r} not available for {node_id} in {node.status}")
            self._event("human_action", f"{approver}: {action}", node_id, data=payload)
            self._save()
            return res

    def _feedback_doc(self, node: Node, approver: str, text: str, kind: str) -> str:
        n = len([f for f in node.feedback if f["source"] == kind]) + 1
        path = f"tasks/{node.task['id']}/{kind}/{n}.md" if node.kind == BUILD \
            else f"control/{kind}/{_slug(node.id)}-{n}.md"
        return self._cwrite(path, f"# {kind} for {node.id}\n\nBy: {approver}\n\n{text}\n").attestation

    async def _human_gate(self, node: Node, action: str, approver: str, payload: dict) -> dict:
        ch = self.change
        text = (payload.get("feedback") or "").strip()
        if node.id == H_REQUIREMENTS:
            intake = self.nodes[INTAKE]
            if action == "accept":
                pins = {self.p(p): self._latest(p) for p in ("intake/requirements.md", "intake/risk.json")}
                derived = ch.risk["tier"]
                tier = payload.get("tier") or derived
                if tier not in risk.TIERS:
                    raise WorkflowError(f"unknown tier {tier}")
                if risk.rank(tier) < risk.rank(derived):
                    if not risk.valid_reason(payload.get("reason")):
                        raise WorkflowError("lowering the tier needs a reason of ≥24 chars and more than one word")
                if tier != derived:
                    ch.tier_override = {"from": derived, "to": tier, "reason": payload.get("reason"),
                                        "approver": approver, "at": now_iso()}
                ch.tier = tier
                ch.requirements = guards.requirements_ids(self._doc("intake/requirements.md").content)
                ch.panel = risk.review_panel(tier, ch.risk["scores"])
                att = self._cwrite("control/approvals/requirements.json", {
                    "gate": "H-1", "approver": approver, "at": now_iso(), "pins": pins, "tier": tier,
                    "derived_tier": derived, "override": ch.tier_override}).attestation
                ch.approvals[H_REQUIREMENTS] = {"approver": approver, "at": now_iso(), "pins": pins,
                                                "attestation": att}
                node.status = ACCEPTED
                node.human = ch.approvals[H_REQUIREMENTS]
                self._create_stage_nodes()
                return {"ok": True, "tier": tier, "panel": ch.panel}
            if action == "revise":
                if not text:
                    raise WorkflowError("feedback is required")
                att = self._feedback_doc(intake, approver, text, "feedback")
                intake.cycle += 1
                intake.feedback.append({"cycle": intake.cycle, "source": "feedback", "text": text,
                                        "attestations": [att]})
                intake.status = REWORK
                node.status = PENDING
                return {"ok": True}
        elif node.id == H_SPEC:
            if action == "approve":
                # Each delta spec, the plan and the oracle are separate decisions (may be
                # different approvers); H-2 completes once every subject is approved and fresh.
                try:
                    done = await governance.approve_subjects(
                        self, payload.get("subjects") or "all", approver)
                except governance.GovernanceError as e:
                    raise WorkflowError(str(e))
                status = governance.h2_status(self)
                pending = [x["subject"] for x in status if x["status"] != "approved"]
                if not pending and risk.rank(ch.tier) >= 3:
                    b = ch.attestations.get("independent-baseline")
                    if not b or b["verdict"] != "pass":
                        return {"ok": True, "approved": done, "pending": ["independent-baseline"],
                                "note": "R3: attest independent-baseline (pass) before build starts, "
                                        "then approve again to release the build"}
                if pending:
                    return {"ok": True, "approved": done, "pending": pending}
                node.status = ACCEPTED
                node.human = {"approved_at": governance.last_h2_approval_at(self), "subjects": status}
                return {"ok": True, "approved": done, "pending": []}
            if action == "revise":
                if not text:
                    raise WorkflowError("feedback is required")
                plan = self.nodes[PLAN]
                att = self._feedback_doc(plan, approver, text, "feedback")
                plan.cycle += 1
                plan.feedback.append({"cycle": plan.cycle, "source": "feedback", "text": text,
                                      "attestations": [att]})
                plan.status = REWORK
                for nid in (CRITIQUE, ORACLE, BASELINE):
                    if nid in self.nodes:
                        n = self.nodes[nid]
                        n.status, n.cycle = PENDING, n.cycle + 1
                for b in self._build_nodes():
                    del self.nodes[b.id]
                await self.git.init_integration(ch.id, ch.base_sha)
                ch.integration_head, ch.oracle_sha, ch.oracle = ch.base_sha, None, None
                ch.surface_pins = {}
                node.status = PENDING
                return {"ok": True}
        elif node.id == H_CHANGE:
            head = await self.git.integration_head(ch.id)
            if action == "accept":
                if head != ch.integration_head:
                    raise WorkflowError("integration head moved; refresh")
                att = self._cwrite("control/approvals/change.json", {
                    "gate": "H-3", "approver": approver, "at": now_iso(), "head": head,
                    "note": text}).attestation
                ch.approvals[H_CHANGE] = {"approver": approver, "at": now_iso(), "head": head,
                                          "attestation": att}
                node.status = ACCEPTED
                node.human = ch.approvals[H_CHANGE]
                return {"ok": True}
            if action == "reject":
                ch.status = "ABANDONED"
                node.status = FAILED
                return {"ok": True}
        raise WorkflowError(f"action {action!r} not valid for {node.id}")

    def _resolve_escalation(self, node: Node, action: str, approver: str, payload: dict) -> dict:
        esc = node.escalation or {}
        text = (payload.get("answer") or payload.get("feedback") or "").strip()
        if action == "abandon":
            node.status = FAILED
            self.change.status = "ABANDONED"
            return {"ok": True}
        if action == "override_accept":
            if node.kind != BUILD:
                raise WorkflowError("override is only available for build tasks")
            if not risk.valid_reason(payload.get("reason")):
                raise WorkflowError("override needs a reason of ≥24 chars and more than one word")
            ver = node.verification.get(str(node.cycle))
            if not node.candidate_sha or not ver or not ver["ok"] or ver["sha"] != node.candidate_sha:
                raise WorkflowError("override requires passing deterministic verification at the "
                                    "candidate (deterministic_verification is not waivable)")
            node.human = {"override": True, "approver": approver, "reason": payload["reason"],
                          "at": now_iso(), "sha": node.candidate_sha}
            node.escalation = None
            node.status = MERGING
            self._spawn(self._merge_node(node.id))
            return {"ok": True}
        if action not in ("retry", "answer"):
            raise WorkflowError(f"unknown escalation action {action!r}")
        if action == "answer":
            if not text:
                raise WorkflowError("an answer is required")
            target = self.nodes[PLAN] if esc.get("resume") == "critique_budget" else node
            att = self._feedback_doc(target, approver, text, "decisions")
            target.feedback.append({"cycle": target.cycle, "source": "decisions",
                                    "text": f"human decision: {text}", "attestations": [att]})
        resume = esc.get("resume", READY)
        node.escalation = None
        if resume == "critique_budget":
            node.extra_cycles += 1
            plan = self.nodes[PLAN]
            plan.cycle += 1
            plan.feedback += node.feedback[-1:]
            plan.status = REWORK
            node.cycle += 1
            node.status = PENDING
        elif resume == "rework_cycle":
            node.extra_cycles += 1
            cur = self.runs.get(node.current_run) if node.current_run else None
            if cur and cur.status == R_REPORTED:
                cur.status = R_SUPERSEDED
            node.cycle += 1
            node.candidate_sha = None
            node.status = REWORK
        elif resume == VERIFYING:
            node.status = VERIFYING
            self._spawn(self._verify_node(node.id))
        elif resume == REVIEWING:
            node.status = REVIEWING
        else:
            node.status = resume if resume in (READY, REWORK) else READY
        return {"ok": True, "status": node.status}

    async def change_action(self, action: str, approver: str, payload: dict | None = None) -> dict:
        """Change-level governance: attest, override, approve_protected_paths, approve_surface."""
        payload = payload or {}
        async with self.lock:
            self._check_approver(approver)
            try:
                if action == "attest":
                    res = await governance.record_attestation(self, approver, payload)
                elif action == "override":
                    res = await governance.record_override(self, approver, payload)
                elif action == "approve_protected_paths":
                    res = governance.record_protected_approval(self, approver, payload)
                elif action == "approve_surface":
                    if not risk.valid_reason(payload.get("reason")):
                        raise governance.GovernanceError("a reason of ≥24 chars is required to re-affirm")
                    res = {"ok": True, "surfaces": await governance.pin_surfaces(
                        self, approver, payload.get("surface"), payload["reason"])}
                else:
                    raise WorkflowError(f"unknown change action {action!r}")
            except governance.GovernanceError as e:
                raise WorkflowError(str(e))
            self._event("human_action", f"{approver}: {action}", data=payload)
            self._save()
            return res

    # ================================================================== ship
    async def _ship(self) -> None:
        from .ship import evaluate
        result = await evaluate(self)
        async with self.lock:
            att = self._cwrite("control/ship/readiness.json", result).attestation
            result["attestation"] = att
            self.change.ship = result
            self.change.status = "READY" if result["verdict"] == "READY" else "NOT_READY"
            node = self.nodes[SHIP]
            node.status = ACCEPTED
            node.human = {"verdict": result["verdict"]}
            self._event("ship", f"readiness verdict: {result['verdict']}", SHIP, data=result["gates"])
            self._save()

    # ================================================================== views
    def snapshot(self) -> dict:
        from dataclasses import asdict
        ch = self.change
        nodes = []
        for n in self.nodes.values():
            d = asdict(n)
            d["feedback_count"] = len(n.feedback)
            nodes.append(d)
        runs = []
        for r in self.runs.values():
            d = asdict(r)
            d.pop("assignment")
            runs.append(d)
        return {"project": self.cfg.project, "repo": self.cfg.repo.name,
                "decision_owners": self.cfg.decision_owners, "version": self.ctl.version,
                "assets_sha": self.assets.sha, "limits": self.cfg.limits.__dict__,
                "change": asdict(ch) if ch else None, "nodes": nodes, "runs": runs,
                "gates": governance.gate_set(self) if ch and ch.tier else [],
                "h2": governance.h2_status(self) if ch and ch.requirements else [],
                "surfaces": governance.surfaces(self) if ch else [],
                "attestation_kinds": sorted(governance.ATTESTATION_KINDS)}
