"""ChangeCore: one change's state and the record helpers every part of the engine uses.

It implements :class:`~legion_orch.workflow.view.ChangeView` (what governance and ship see) and
is handed to each engine collaborator; it knows nothing of dispatch, reports or verification."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ...store import DocVersion
from ...util import now_iso
from .. import attention, attest, governance
from ..attention import AttentionItem
from ..model import (
    ACCEPTED,
    BUILD,
    Change,
    Node,
    Oracle,
    Run,
)
from .common import log
from .ids import base_id
from .services import EngineHost, Services
from .state import StateStore

if TYPE_CHECKING:
    pass


def _waited(since: str) -> str:
    """How long an item waited, e.g. "9h02m" (empty when unknown)."""
    if not since:
        return ""
    try:
        start = datetime.strptime(since, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return ""
    s = max(0, int((datetime.now(UTC) - start).total_seconds()))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


class ChangeCore:
    """State (through the :class:`StateStore`), documents and events of one change."""

    def __init__(self, svc: Services, host: EngineHost, cid: str):
        self.svc, self.host, self.cid = svc, host, cid
        self.cfg, self.docs, self.git = svc.cfg, svc.docs, svc.git
        self.assets, self.runner, self.lock = svc.assets, svc.runner, svc.lock
        self.procs = svc.procs
        self.st = StateStore(svc.docs, cid)
        self._timeline: list[str] = []                 # events not yet written to the store
        self._timeline_at = time.monotonic()
        self._statuses: dict[str, str] = {}            # node statuses as of the last save
        self._attention: dict[str, AttentionItem] = {}  # attention items as of the last save
        self._saved = asyncio.Event()                  # set (and replaced) on every save

    # ------------------------------------------------------------------ state
    @property
    def change(self) -> Change | None:
        return self.st.change

    @property
    def nodes(self) -> dict[str, Node]:
        return self.st.nodes

    @property
    def runs(self) -> dict[str, Run]:
        return self.st.runs

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def current(self, base: str) -> Node | None:
        """The node ``base`` (``oracle``, ``H-2`` …) of the current fix round, or of the latest
        round that has one (a round re-makes only some of the plan's steps)."""
        found = [n for nid, n in self.nodes.items() if base_id(nid) == base]
        return max(found, key=lambda n: (n.round <= self.change.round, n.round), default=None) \
            if self.change else None

    def build_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == BUILD]

    def setup_waits_for_scaffold(self) -> bool:
        """A blank repository (§5.3b, §5.3c): until the scaffold is accepted, what the setup task
        needs does not exist – no sandbox runs it, and provisioning does not check it; the
        scaffold's own verification runs it, at its candidate."""
        if not self.change.blank_repo:
            return False
        s = self.scaffold_node()
        return s is None or s.status != ACCEPTED

    def scaffold_node(self) -> Node | None:
        """The blank repository's scaffold build node (§5.3b), if the plan has one."""
        return next((n for n in self.build_nodes() if n.task and n.task.scaffold), None)

    def save(self) -> None:
        """Persist the state (the caller holds the lock) and wake what waits on it and the UI. Every
        state change passes through here, so this is where status changes are stamped and
        attention items are raised and resolved (§2.2) – and where the workflow (the stage
        rules) sees what changed (:meth:`sync`)."""
        self.before_save()
        self._stamp_statuses()
        self._update_attention()
        self.st.save()
        self.flush_timeline(force=self.change is not None and self.change.status != "ACTIVE")
        self.notify()
        self.host.changed()

    def notify(self) -> None:
        """Wake what waits on the change's state (:meth:`next_save`)."""
        saved, self._saved = self._saved, asyncio.Event()
        saved.set()

    async def next_save(self) -> None:
        """Return at the change's next save: what a phase waits for may hold now."""
        await self._saved.wait()

    def before_save(self) -> None:
        """Hook: the workflow's own record, saved with the change."""

    # ------------------------------------------------------------------ attention (§2.2)
    @property
    def attention_items(self) -> list[AttentionItem]:
        """The change's attention items as of the last save."""
        return list(self._attention.values())

    def seed_attention(self) -> None:
        """After loading from the store: take the current statuses and items as known, so a
        restart neither re-stamps nor re-raises (nobody is notified again for old items)."""
        self._statuses = {nid: n.status for nid, n in self.nodes.items()}
        self._attention = {i.key: i for i in attention.items(self)}

    def _stamp_statuses(self) -> None:
        now = now_iso()
        for nid, n in self.nodes.items():
            if self._statuses.get(nid) != n.status:
                n.status_since = now
        self._statuses = {nid: n.status for nid, n in self.nodes.items()}

    def _update_attention(self) -> None:
        cur = {i.key: i for i in attention.items(self)}
        for key in cur.keys() - self._attention.keys():
            item = cur[key]
            self.event("attention", f"raised: {item.title}", item.node)
            self.host.attention(item, raised=True)
        for key in self._attention.keys() - cur.keys():
            item = self._attention[key]
            waited = _waited(item.since)
            self.event("attention", f"resolved{f' after {waited}' if waited else ''}: {item.title}",
                       item.node)
            self.host.attention(item, raised=False)
        self._attention = cur

    def head_moved(self, branch_head: str) -> str | None:
        """G-H1: the integration branch is written only by the controller. If it is not at the
        head the controller recorded, someone committed outside the workflow: the reason to
        escalate with (None: it is where it should be)."""
        rec = self.change.integration_head
        if branch_head == rec:
            return None
        return (f"G-H1: the integration branch {self.git.integration_branch(self.cid)} is at "
                f"{branch_head[:12]}, but the controller's integration head is {rec[:12]}: "
                "commits were added outside the workflow. They are not adopted (they bypass "
                f"verification and review): reset the branch to {rec} and retry. To change "
                "the code after integration fails, plan a fix round (Plan a fix).")

    def oracle_commands(self) -> list[str]:
        """The commands that verify the integration head (oracle and surfaces)."""
        orc = self.change.oracle or Oracle()
        cmds = orc.acceptance_commands + orc.integration_commands \
            + governance.surface_commands(self)
        return list(dict.fromkeys(cmds))

    def baseline_commands(self) -> list[str]:
        """The commands the baseline runs before any build: acceptance and integration."""
        orc = self.change.oracle or Oracle()
        return list(dict.fromkeys(orc.acceptance_commands + orc.integration_commands))

    # ------------------------------------------------------------------ events and tasks
    def event(self, kind: str, message: str, node: str | None = None,
              run: str | None = None) -> None:
        """Log a notable step, and append it to the change's timeline (a retrospective reads it
        alongside the store's records, which are the history)."""
        log.info("[%s %s] %s%s", self.cid, kind, f"{node}: " if node else "", message)
        self._append_timeline({"ts": now_iso(), "kind": kind, "node": node, "run": run,
                               "message": message})
        self.host.touch()

    TIMELINE_FLUSH_EVENTS, TIMELINE_FLUSH_S = 200, 60.0

    def _append_timeline(self, rec: dict) -> None:
        self._timeline.append(json.dumps(rec, default=str))

    def flush_timeline(self, force: bool = False) -> None:
        """Write the buffered events as the next ``control/timeline/<n>.jsonl`` chunk: on save
        once enough have built up or a minute has passed, and when ``force``d (the change ended, a
        retrospective needs it). A crash loses at most the unwritten chunk."""
        if not self._timeline or self.change is None:
            return
        if not force and len(self._timeline) < self.TIMELINE_FLUSH_EVENTS \
                and time.monotonic() - self._timeline_at < self.TIMELINE_FLUSH_S:
            return
        n = len(self.docs.list(self.p("control/timeline/"))) + 1
        self.write_record(f"control/timeline/{n:05d}.jsonl", "\n".join(self._timeline) + "\n")
        self._timeline, self._timeline_at = [], time.monotonic()

    def timeline(self) -> str:
        """The change's events so far (JSON lines)."""
        self.flush_timeline(force=True)
        return "".join(self.docs.read(m.path, m.version).content
                       for m in self.docs.list(self.p("control/timeline/")))

    def timeline_events(self, since: str = "") -> list[dict]:
        """The change's events at or after ``since`` (ISO), stored and still buffered, without
        writing a chunk (unlike :meth:`timeline`): for read-only views such as the digest."""
        out = []
        chunks = [self.docs.read(m.path, m.version).content
                  for m in self.docs.list(self.p("control/timeline/"))]
        for line in "".join(chunks).splitlines() + self._timeline:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and str(ev.get("ts", "")) >= since:
                out.append(ev)
        return out

    def spawn(self, coro) -> None:
        """Run a background task; the controller logs a failure, the event marks the change."""
        self.host.spawn(coro, on_error=lambda e: self.event("error", f"internal error: {e}"))

    # ------------------------------------------------------------------ documents
    @property
    def git_url(self) -> str:
        return self.svc.git_url

    @property
    def mcp_url(self) -> str:
        return self.svc.mcp_url

    def p(self, rel: str) -> str:
        """Qualify a document path into this change's namespace."""
        return attest.qualify(rel, self.cid)

    def write_record(self, rel: str, content: str | dict | BaseModel) -> DocVersion:
        path = self.p(rel)
        if isinstance(content, BaseModel):
            content = content.model_dump(mode="json", exclude_none=True)
        if not isinstance(content, str):
            content = json.dumps(content, indent=2, sort_keys=True)
        return self.docs.write(path, content, author="controller", role="controller",
                               base_version=self.docs.latest_version(path))

    def latest(self, rel: str) -> int | None:
        return self.docs.latest_version(self.p(rel))

    def doc(self, rel: str, version: int | None = None) -> DocVersion:
        return self.docs.read(self.p(rel), version)

    def latest_att(self, rel: str) -> str | None:
        v = self.latest(rel)
        return f"file:{self.p(rel)}@{v}" if v else None

    def json_doc(self, rel: str, version: int | None = None) -> Any:
        return json.loads(self.doc(rel, version).content)

    def run_dir(self, run_id: str) -> Path:
        """Scratch only: a run's payload (deleted after upload) and its live agent log."""
        return self.svc.tmp / "runs" / run_id
