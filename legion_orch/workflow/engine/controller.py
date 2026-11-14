"""Controller: shared resources, scheduler loop, sandbox identity binding."""
from __future__ import annotations

import asyncio
import secrets
import shutil
import sqlite3
import threading
from datetime import UTC, datetime

import httpx

from ...config import Config
from ...git import GitGrant, GitRemote
from ...identity import JWT_SPIFFE, AccessTokens, DevSpire, IdentityService, SvidVerifier
from ...sandbox import (
    LocalRunner,
    Runner,
    make_runner,
)
from ...store import DocStore
from ..assets import Assets
from ..model import (
    ACCEPTED,
    H_REQUIREMENTS,
    HUMAN,
    INTAKE,
    LIVE_RUN,
    Change,
    Node,
    Run,
)
from ..roles import REGISTRY as ROLE_HANDLERS
from ..roles import handler_for
from .change import ChangeEngine
from .common import WorkflowError, log


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
        unhandled = set(self.assets.roles) - set(ROLE_HANDLERS)
        if unhandled:
            raise RuntimeError(f"roles without a handler in workflow/roles/: {sorted(unhandled)}")
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
                    ce.event("error", f"scheduler error: {e}")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0)
            except TimeoutError:
                pass

    # ------------------------------------------------------------------ changes
    async def start_change(self, goal: str) -> ChangeEngine:
        goal = goal.strip()
        if not goal:
            raise WorkflowError("goal is required")
        async with self.lock:
            base = await self.git.resolve_base()
            while True:
                cid = "c" + datetime.now(UTC).strftime("%y%m%d") + "-" + secrets.token_hex(2)
                if cid not in self.changes:
                    break
            await self.git.init_integration(cid, base)
            ce = ChangeEngine(self, cid)
            ce.st.change = Change(id=cid, goal=goal, base_sha=base, integration_head=base)
            self.changes[cid] = ce
            ce.write_record("control/goal.md", f"# Goal\n\n{goal}\n\nBase: `{base}`\n")
            ce._add(Node(INTAKE, INTAKE, "Intake: requirements & risk"))
            ce._add(Node(H_REQUIREMENTS, HUMAN, "Accept requirements & tier", depends_on=[INTAKE]))
            ce.event("change", f"change {cid} started at {base[:12]}", data={"goal": goal})
            ce._save()
            return ce

    def get(self, cid: str) -> ChangeEngine:
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

    def find_run(self, run_id: str) -> tuple[ChangeEngine, Run]:
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

    def run_for_bearer(self, token: str | None) -> tuple[ChangeEngine, Run] | None:
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
        return GitGrant(run.id, f"refs/heads/{run.branch}" if handler_for(run.role).pushes else None)
