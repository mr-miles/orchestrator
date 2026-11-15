"""Controller: shared resources, scheduler loop, sandbox identity binding."""
from __future__ import annotations

import asyncio
import secrets
import shutil
import sqlite3
import threading
from datetime import UTC, datetime

from ...config import Config
from ...git import GitRemote
from ...sandbox import (
    Runner,
    make_runner,
)
from ...store import DocStore
from ..assets import Assets
from ..model import (
    LIVE_RUN,
    Run,
)
from ..roles import REGISTRY as ROLE_HANDLERS
from .auth import WorkloadAuth
from .change import ChangeEngine
from .common import WorkflowError, log
from .services import Services


class Controller:
    """Owns the shared :class:`Services`, one :class:`ChangeEngine` per change, and the scheduler
    loop. Implements :class:`EngineHost` for the engines.

    Durable state lives only in the document store (``state.db``) and the git remote; ``tmp/``
    holds transient sandbox payloads and live agent logs and is wiped on start.
    """

    def __init__(self, cfg: Config, runner: Runner | None = None):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(cfg.state_dir / "state.db", check_same_thread=False)
        assets = Assets(cfg.assets_dir)
        unhandled = set(assets.roles) - set(ROLE_HANDLERS)
        if unhandled:
            raise RuntimeError(f"roles without a handler in workflow/roles/: {sorted(unhandled)}")
        self.changes: dict[str, ChangeEngine] = {}
        self.auth = WorkloadAuth(cfg, self._runs_by_change)
        runner = runner or make_runner(cfg, self.auth.dev_gateway if self.auth.dev_spire else None)
        self.svc = Services(
            cfg=cfg, docs=DocStore(conn, threading.RLock(), assets.schemas),
            git=GitRemote(cfg.state_dir / "git", cfg.repo.name, cfg.repo.path, cfg.repo.base_branch,
                          max_push_bytes=cfg.limits.git_max_push_bytes),
            assets=assets, runner=runner, auth=self.auth, tmp=cfg.state_dir / "tmp")
        self.version = 0                  # bumped on every visible change (UI polling)
        self.stopping = asyncio.Event()   # set on shutdown: long-lived responses end
        self._tasks: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None

    # The pieces the HTTP layer reads.
    @property
    def docs(self) -> DocStore:
        return self.svc.docs

    @property
    def git(self) -> GitRemote:
        return self.svc.git

    @property
    def assets(self) -> Assets:
        return self.svc.assets

    @property
    def git_url(self) -> str:
        return self.svc.git_url

    def set_mcp_tools(self, tools: list[str]) -> None:
        """The MCP server's tool list: the gateway allowlist in every sandbox policy."""
        self.svc.mcp_tools = list(tools)

    # ------------------------------------------------------------------ EngineHost
    def changed(self) -> None:
        self.version += 1
        self._wake.set()

    def touch(self) -> None:
        self.version += 1

    def spawn(self, coro, on_error=None) -> None:
        async def wrapper():
            try:
                await coro
            except Exception as e:  # never lose a background failure silently
                log.exception("background task failed")
                if on_error:
                    on_error(e)
        t = asyncio.create_task(wrapper())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ lifecycle
    def load(self) -> None:
        """Rebuild every change from the store."""
        for m in self.docs.list("changes/"):
            parts = m.path.split("/")
            if len(parts) == 4 and parts[2:] == ["control", "change.json"]:
                ce = ChangeEngine(self.svc, self, parts[1])
                ce.load()
                self.changes[ce.cid] = ce

    async def start(self) -> None:
        await self.git.ensure()
        shutil.rmtree(self.svc.tmp, ignore_errors=True)   # stale payloads may contain old tokens
        self.svc.tmp.mkdir(parents=True)
        self.load()
        async with self.svc.lock:
            for ce in self.changes.values():
                ce.recover()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
        for p in self.svc.procs.values():
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
        async with self.svc.lock:
            base = await self.git.resolve_base()
            while True:
                cid = "c" + datetime.now(UTC).strftime("%y%m%d") + "-" + secrets.token_hex(2)
                if cid not in self.changes:
                    break
            await self.git.init_integration(cid, base)
            ce = ChangeEngine(self.svc, self, cid)
            self.changes[cid] = ce
            ce.start(goal, base)
            return ce

    def get(self, cid: str) -> ChangeEngine:
        try:
            return self.changes[cid]
        except KeyError:
            raise WorkflowError(f"unknown change {cid}") from None

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

    def _runs_by_change(self):
        for ce in self.changes.values():
            for r in ce.runs.values():
                yield ce.cid, r

    def run_for_bearer(self, token: str | None) -> tuple[ChangeEngine, Run] | None:
        """The change and live run a gateway-injected bearer acts for (see WorkloadAuth)."""
        found = self.auth.live_run(token)
        return (self.changes[found[0]], found[1]) if found else None
