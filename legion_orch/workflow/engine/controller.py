"""Controller: shared resources (the dispatcher among them), sandbox identity binding."""
from __future__ import annotations

import asyncio
import secrets
import shutil
import sqlite3
import threading
from datetime import UTC, datetime

from ...bus import Bus, DocWritten, Pushed
from ...config import Config
from ...git import GitRemote
from ...identity import WorkloadBinding
from ...notify import Notifier
from ...sandbox import (
    CHANGE_LABEL,
    EnvironmentBuilder,
    Listing,
    Runner,
    SandboxError,
    human_name,
    make_builder,
    make_runner,
)
from ...sandbox.templates import label_value
from ...store import DocStore
from ...store.journal import Journal
from ...store.state_tables import StateTables
from ...telemetry import Telemetry
from ...util import stop_process
from ..assets import Assets
from ..attention import AttentionItem
from ..environment import Library
from ..model import (
    LIVE_RUN,
    Run,
    Verification,
)
from ..roles import REGISTRY as ROLE_HANDLERS
from .base.auth import WorkloadAuth
from .base.common import WorkflowError, log
from .base.dispatch import Dispatcher, PriorityCap
from .base.shared import Services
from .change import ChangeEngine


class Controller:
    """Owns the shared :class:`Services`, one :class:`ChangeEngine` per change, and the timer
    of the attention reminders. Implements :class:`EngineHost` for the engines. There is no
    clock: each change's work runs as tasks that await what they need.

    Durable state lives only in ``state.db`` (the documents, and the changes' live state:
    :class:`StateStore`), the changes' journals (``journal/``, their history) and the git
    remote; ``tmp/``
    holds transient sandbox payloads and live agent logs and is wiped on start.
    """

    def __init__(self, cfg: Config, runner: Runner | None = None,
                 builder: EnvironmentBuilder | None = None,
                 telemetry: Telemetry | None = None):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(cfg.state_dir / "state.db", check_same_thread=False)
        assets = Assets(cfg.assets_dir)
        unhandled = set(assets.roles) - set(ROLE_HANDLERS)
        if unhandled:
            raise RuntimeError(f"roles without a handler in workflow/roles/: {sorted(unhandled)}")
        self.changes: dict[str, ChangeEngine] = {}
        self.bus = Bus(self.spawn)          # notifications between components (bus.py)
        self.auth = WorkloadAuth(cfg, self._runs_by_change, self._workspace_of)
        runner = runner or make_runner(cfg, self.auth.dev_gateway if self.auth.dev_spire else None)
        lock = threading.RLock()            # the database's (documents and state tables)
        self.svc = Services(
            cfg=cfg, docs=DocStore(conn, lock, assets.schemas),
            tables=StateTables(conn, lock), journal=Journal(cfg.state_dir / "journal"),
            hubs={w.name: GitRemote(cfg.state_dir / "git", w.name, w.source, w.base_branch,
                                    max_push_bytes=cfg.limits.git_max_push_bytes)
                  for w in cfg.workspaces},
            assets=assets, runner=runner, auth=self.auth, tmp=cfg.state_dir / "tmp", bus=self.bus,
            builder=builder or make_builder(cfg), dispatcher=Dispatcher(cfg.limits.max_parallel),
            verify_cap=PriorityCap(cfg.limits.max_parallel_verifications),
            telemetry=telemetry or Telemetry.from_config(cfg))
        self.svc.docs.on_write.append(self._doc_written)   # projections follow the documents
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
        self.a2a = None                     # the A2A transport (server/a2a.py), once it is up

    def _doc_written(self, meta) -> None:
        """A change's document has a new version: tell the change (its projections)."""
        parts = meta.path.split("/", 2)
        if len(parts) == 3 and parts[0] == "changes":
            self.bus.publish(DocWritten(parts[1], parts[2], meta.version, meta.schema))

    # The pieces the HTTP layer reads.
    @property
    def docs(self) -> DocStore:
        return self.svc.docs

    def hub(self, workspace: str) -> GitRemote:
        """A configured workspace's hub (``LookupError`` if there is none of that name)."""
        return self.svc.hub(workspace)

    @property
    def hubs(self) -> dict[str, GitRemote]:
        return self.svc.hubs

    @property
    def assets(self) -> Assets:
        return self.svc.assets

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

    # ---- the A2A layer's inbox and transport (server/inbox.py, server/a2a.py)
    def _a2a(self):
        if self.a2a is None:
            raise WorkflowError("no A2A listener: agents cannot be messaged")
        return self.a2a

    async def deliver(self, cid, card, sender, sender_role, text, task_id):
        return await self._a2a().inbox.deliver(cid, card, sender, sender_role, text, task_id)

    def inbox_messages(self, cid, card):
        return [(i.task_id, i.sender, i.text) for i in self._a2a().inbox.pending(cid, card)
                if not i.reply.done()]                 # (answered: its sender takes it out)

    def reply_message(self, cid, card, task_id, state, text):
        return self._a2a().inbox.reply(cid, card, task_id, state, text)

    def fail_message(self, cid, card, task_id, why):
        self._a2a().inbox.fail(cid, card, task_id, why)



    def replay_cards(self) -> None:
        """Every change publishes again the cards it has open (a catalog subscribed late: the
        controller started)."""
        for ce in self.changes.values():
            ce.republish_cards()

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
        for cid in self.svc.tables.changes():
            ce = ChangeEngine(self.svc, self, cid)
            ce.load()
            if ce.change.workspace not in self.hubs:      # its workspace left the config
                log.warning("change %s is in workspace %r, which is not configured: not "
                            "loaded", ce.cid, ce.change.workspace)
                continue
            self.changes[ce.cid] = ce
            self.notifier.seed(ce.attention_items)

    async def start(self) -> None:
        for hub in self.hubs.values():   # configure the hubs there are; a change clones one
            if hub.exists or hub.source is None:
                await hub.ensure()
        shutil.rmtree(self.svc.tmp, ignore_errors=True)   # stale payloads may contain old tokens
        self.svc.tmp.mkdir(parents=True)
        self.load()
        await self.adopt_sandboxes()
        async with self.svc.lock:
            for ce in self.changes.values():
                ce.recover()
        self._loop_task = asyncio.create_task(self._remind())

    async def adopt_sandboxes(self) -> None:
        """After :meth:`load`: each change's sandboxes as the gateway lists them (those labelled
        ``legion.change=<id>``) – which exist, which are stopped (not recorded: the gateway's
        word). A change whose listing failed keeps what it has."""
        for ce in list(self.changes.values()):
            listing = await self._listing(ce.cid)
            if listing is None:
                continue
            async with self.svc.lock:
                ce.adopt_sandboxes({s.name: s for s in listing.sandboxes}, listing.complete)

    async def _listing(self, cid: str) -> Listing | None:
        try:
            listing = await self.svc.runner.list({CHANGE_LABEL: label_value(cid)})
        except (SandboxError, ValueError, OSError) as e:
            log.warning("listing change %s's sandboxes failed: %s", cid, e)
            return None
        if not listing.complete:
            log.warning("change %s's sandboxes did not list to the end: missing ones are not "
                        "taken to be gone", cid)
        return listing

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
        await asyncio.to_thread(self.svc.telemetry.shutdown)

    REMIND_S = 60

    async def _remind(self) -> None:
        """The attention reminders' timer (the notifier decides what is due)."""
        while True:
            self.notifier.remind(self.attention_items())
            await asyncio.sleep(self.REMIND_S)

    # ------------------------------------------------------------------ changes
    async def start_change(self, goal: str, workspace: str) -> ChangeEngine:
        """A new change in ``workspace``. (No workspace is kept for a change across workspaces,
        planned by an agent in several places – not supported yet.)"""
        goal = goal.strip()
        if not goal:
            raise WorkflowError("goal is required")
        if not workspace:
            raise WorkflowError(f"workspace is required: one of {', '.join(self.hubs)} (a change "
                                "across workspaces is not supported yet)")
        if workspace not in self.hubs:
            raise WorkflowError(f"unknown workspace {workspace!r} (one of {', '.join(self.hubs)})")
        async with self.svc.lock:
            while True:
                cid = "c" + datetime.now(UTC).strftime("%y%m%d") + "-" + secrets.token_hex(2)
                if cid not in self.changes:
                    break
            ce = ChangeEngine(self.svc, self, cid)
            self.changes[cid] = ce
            ce.start(goal, workspace)               # its first step syncs the workspace
            return ce

    def get(self, cid: str) -> ChangeEngine:
        try:
            return self.changes[cid]
        except KeyError:
            raise WorkflowError(f"unknown change {cid}") from None

    # ------------------------------------------------------------------ runs / tokens
    def _workspace_of(self, cid: str) -> str | None:
        ce = self.changes.get(cid)
        return ce.change.workspace if ce is not None and ce.change is not None else None

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

    def find_verification_log(self, log_id: str) -> tuple[ChangeEngine, Verification | None]:
        """The change whose verification has the log ``log_id``, and the verification (None:
        it is still running)."""
        for ce in self.changes.values():
            if log_id in ce.verifying.values():
                return ce, None
            for node in ce.nodes.values():
                for v in node.verification.values():
                    if v.log == log_id:
                        return ce, v
        raise WorkflowError(f"unknown run {log_id}")

    def _runs_by_change(self):
        for ce in self.changes.values():
            for r in ce.runs.values():
                yield ce.cid, r

    async def git_pushed(self, grant) -> None:
        """A run pushed its branch (the git remote, after git accepted it): the change verifies
        the new tip."""
        try:
            ce, run = self.find_run(grant.identity)
        except WorkflowError:
            return                                     # a verifier: it never pushes
        if not run.branch:
            return
        sha = await ce.git.ref_sha(f"refs/heads/{run.branch}")
        if sha:
            self.bus.publish(Pushed(ce.cid, run.id, sha))   # the verification service verifies it

    def a2a_caller(self, token: str | None) -> tuple[ChangeEngine, WorkloadBinding] | None:
        """The change and agent (sandbox, task, role) an A2A request's bearer comes from."""
        b = self.auth.a2a_caller(token)
        ce = self.changes.get(b.change) if b else None
        return (ce, b) if ce is not None else None

    def run_for_bearer(self, token: str | None) -> tuple[ChangeEngine, Run] | None:
        """The change and live run a gateway-injected bearer acts for (see WorkloadAuth)."""
        found = self.auth.live_run(token)
        return (self.changes[found[0]], found[1]) if found else None
