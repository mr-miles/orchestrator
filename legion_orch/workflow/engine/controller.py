"""Controller: shared resources (the dispatcher among them), sandbox identity binding."""
from __future__ import annotations

import asyncio
import secrets
import shutil
import sqlite3
import threading
from datetime import UTC, datetime

from ...config import Config
from ...git import GitRemote, is_blank_tree
from ...notify import Notifier
from ...sandbox import (
    EnvironmentBuilder,
    Runner,
    human_name,
    make_builder,
    make_runner,
)
from ...store import DocStore
from ...util import stop_process
from .. import setup
from ..assets import Assets
from ..attention import AttentionItem
from ..environment import Library
from ..model import (
    LIVE_RUN,
    Run,
)
from ..roles import REGISTRY as ROLE_HANDLERS
from .auth import WorkloadAuth
from .change import ChangeEngine
from .common import WorkflowError, log
from .dispatch import Dispatcher
from .services import Services


class Controller:
    """Owns the shared :class:`Services`, one :class:`ChangeEngine` per change, and the timer
    of the attention reminders. Implements :class:`EngineHost` for the engines. There is no
    clock: each change's work runs as tasks that await what they need.

    Durable state lives only in the document store (``state.db``) and the git remote; ``tmp/``
    holds transient sandbox payloads and live agent logs and is wiped on start.
    """

    def __init__(self, cfg: Config, runner: Runner | None = None,
                 builder: EnvironmentBuilder | None = None):
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
            assets=assets, runner=runner, auth=self.auth, tmp=cfg.state_dir / "tmp",
            builder=builder or make_builder(cfg), dispatcher=Dispatcher(cfg.limits.max_parallel))
        if cfg.environment is not None:   # fail fast: every change would escalate on it
            Library.for_config(cfg, assets.schemas)
        self.version = 0                  # bumped on every visible change (UI streams)
        self._bumped = asyncio.Event()    # set (and replaced) whenever the version moves
        self._reserved_names: set[str] = set()   # sandbox names handed out this process
        self.stopping = asyncio.Event()   # set on shutdown: long-lived responses end
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        self.notifier = Notifier(cfg.notify, cfg.project, cfg.decision_owners, self.spawn,
                                 self._notified)

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

    # ------------------------------------------------------------------ EngineHost
    def changed(self) -> None:
        self._bump()

    def touch(self) -> None:
        self._bump()

    def attention(self, item: AttentionItem, raised: bool) -> None:
        if raised:
            self.notifier.raised(item)
        else:
            self.notifier.resolved(item)

    def _notified(self, item: AttentionItem, event: str, sink: str, error: str | None) -> None:
        ce = self.changes.get(item.change)
        if ce is not None:
            ce.event("notified", f"{event} via {sink}: " + (f"failed: {error}" if error else "sent")
                     + f" ({item.title})", item.node)

    def attention_items(self) -> list[AttentionItem]:
        """Every change's attention items (§2.2): blocking first, oldest first."""
        out = [i for ce in self.changes.values() for i in ce.attention_items]
        return sorted(out, key=lambda i: (not i.blocking, i.since))

    def _bump(self) -> None:
        self.version += 1
        bumped, self._bumped = self._bumped, asyncio.Event()
        bumped.set()

    async def wait_version(self, seen: int, timeout: float) -> None:
        """Return once the version differs from ``seen``, the controller stops, or ``timeout``."""
        if self.version != seen or self.stopping.is_set():
            return
        waits = [asyncio.ensure_future(self._bumped.wait()),
                 asyncio.ensure_future(self.stopping.wait())]
        try:
            await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()

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
                self.notifier.seed(ce.attention_items)

    async def start(self) -> None:
        await self.git.ensure()
        shutil.rmtree(self.svc.tmp, ignore_errors=True)   # stale payloads may contain old tokens
        self.svc.tmp.mkdir(parents=True)
        self.load()
        async with self.svc.lock:
            for ce in self.changes.values():
                ce.recover()
        self._loop_task = asyncio.create_task(self._remind())

    STOP_WAIT_S = 10

    async def stop(self) -> None:
        """Stop the reminders, kill the agents and cancel background work (reflections, deletions,
        provisioning), then wait for all of it, so no child process outlives the controller."""
        if self._loop_task:
            self._loop_task.cancel()
        procs = list(self.svc.procs.values())
        for p in procs:
            if p.returncode is None:
                p.kill()
        tasks = [t for t in (self._loop_task, *self._tasks) if t is not None]
        for t in tasks:
            t.cancel()
        waits = [asyncio.ensure_future(stop_process(p)) for p in procs] + tasks
        if waits:
            await asyncio.wait(waits, timeout=self.STOP_WAIT_S)
        for ce in self.changes.values():
            ce.flush_timeline(force=True)

    REMIND_S = 60

    async def _remind(self) -> None:
        """The attention reminders' timer (the notifier decides what is due)."""
        while True:
            self.notifier.remind(self.attention_items())
            await asyncio.sleep(self.REMIND_S)

    # ------------------------------------------------------------------ changes
    async def start_change(self, goal: str) -> ChangeEngine:
        goal = goal.strip()
        if not goal:
            raise WorkflowError("goal is required")
        async with self.svc.lock:
            base = await self.git.resolve_base()
            blank = is_blank_tree(await self.git.tree_paths(base))
            # §5.3c: the base's setup task – not in a blank repository, where what a setup needs
            # does not exist yet (it comes with the scaffold, through the environment stage)
            pin = None if blank else await setup.pin_at_base(self.git, base)
            while True:
                cid = "c" + datetime.now(UTC).strftime("%y%m%d") + "-" + secrets.token_hex(2)
                if cid not in self.changes:
                    break
            await self.git.init_integration(cid, base)
            ce = ChangeEngine(self.svc, self, cid)
            self.changes[cid] = ce
            ce.start(goal, base, blank_repo=blank, setup_pin=pin)
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

    def used_sandbox_names(self) -> set[str]:
        """Every sandbox name in the store's history (runs and verifications) or reserved for a
        sandbox being created: a name is never reused, so logs and events stay unambiguous."""
        names = set(self._reserved_names)
        for ce in self.changes.values():
            names.update(r.sandbox for r in ce.runs.values() if r.sandbox)
            for node in ce.nodes.values():
                names.update(v.sandbox for v in node.verification.values() if v.sandbox)
        return names

    def new_sandbox_name(self, prefix: str) -> str:
        used = self.used_sandbox_names()
        name = human_name(prefix, used.__contains__)
        self._reserved_names.add(name)
        return name

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
