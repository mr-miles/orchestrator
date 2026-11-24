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

from ....bus import Escalated, Event
from ....store import DocVersion
from ....util import now_iso
from ... import attention, attest
from ...attention import AttentionItem
from ...attest import FILE_RE
from ...model import Change, Oracle, Run
from ...tickets import Status, Ticket, Tickets
from ...tickets.executors import Work as TicketWork
from .common import log
from .projections import Projections
from .shared import EngineHost, Services
from .state import StateStore

if TYPE_CHECKING:
    from ....git import GitRemote


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
        self.cfg, self.docs = svc.cfg, svc.docs
        self.assets, self.runner, self.lock = svc.assets, svc.runner, svc.lock
        self.procs = svc.procs
        self.st = StateStore(svc.docs, svc.tables, svc.journal, cid)
        self.projections = Projections(self)           # its records copied from documents
        self.trace = svc.telemetry.change(cid)
        self._timeline: list[str] = []                 # events not yet written to the store
        self.verifying: dict[str, str] = {}            # ticket -> its live verification's log
        self._tickets: Tickets | None = None
        self._timeline_at = time.monotonic()
        self._statuses: dict[str, str] = {}            # ticket statuses as of the last save
        self._attention: dict[str, AttentionItem] = {}  # attention items as of the last save
        self._outbox: list[Event] = []                 # events to publish once saved (bus.py)
        self._saved = asyncio.Event()                  # set (and replaced) on every save

    # ------------------------------------------------------------------ state
    @property
    def change(self) -> Change | None:
        return self.st.change

    @property
    def runs(self) -> dict[str, Run]:
        return self.st.runs

    @property
    def tickets(self) -> Tickets:
        """The change's tickets: the rules' records (their dict is the state store's)."""
        if self._tickets is None or self._tickets.t is not self.st.tickets:
            self._tickets = Tickets.over(self.st.tickets)
        return self._tickets

    @property
    def hooks(self) -> dict[str, Ticket]:
        return self.st.hooks

    def ticket(self, tid: str) -> Ticket | None:
        """A ticket of the tree, or of a side process outside it (the retrospective)."""
        return self.st.tickets.get(tid) or self.st.hooks.get(tid)

    def setup_waits_for_scaffold(self) -> bool:
        """A blank repository (§5.3d, §5.3c): until the plan's scaffold has passed, what the
        setup task needs does not exist – no agent's sandbox runs it; the scaffold's own
        verification runs it, at its commit."""
        if not self.change.blank_repo:
            return False
        return not any(t.type == "agent:scaffolder" and t.status == Status.PASSED
                       for t in self.st.tickets.values())

    def pointer_json(self, name: str) -> Any:
        """An approved JSON output (the change's pointer ``name``), or None."""
        ref = self.tickets.root.pointers.get(name) if self.st.tickets else None
        m = FILE_RE.match(ref) if isinstance(ref, str) else None
        if m is None:
            return None
        try:
            return json.loads(self.docs.read(m["path"], int(m["version"])).content)
        except ValueError:
            return None

    def round_of(self, ticket: Ticket) -> TicketWork:
        """A ticket's round as it would start now – for an agent asked to answer a message
        before (or between) its rounds: its attempt, findings and inputs, at its last base."""
        last = next((self.runs[r] for r in reversed(ticket.runs) if r in self.runs), None)
        return TicketWork(ticket=ticket, attempt=max(ticket.attempt, 1),
                          findings=tuple(ticket.findings), change=self.cid,
                          base=(last.base_sha if last else None) or self.change.base_sha,
                          inputs={i: self.tickets.root.pointers.get(i) for i in ticket.inputs})

    def save(self) -> None:
        """Persist the state (the caller holds the lock) and wake what waits on it and the UI. Every
        state change passes through here, so this is where status changes are stamped and
        attention items are raised and resolved (§2.2) – and where the workflow (the stage
        rules) sees what changed (:meth:`sync`)."""
        self.before_save()
        self._stamp_statuses()
        self.trace.tickets(self.st.tickets.values())
        self.trace.runs(self.st.runs.values())
        self._update_attention()
        self.st.save()
        outbox, self._outbox = self._outbox, []
        for event in outbox:                       # after the commit: what it raised (bus.py)
            self.svc.bus.publish(event)
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

    def emit(self, event: Event) -> None:
        """Raise an event (the caller holds the lock): published once the change has saved."""
        self._outbox.append(event)

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
        self._statuses = self._statuses_now()
        self._attention = {i.key: i for i in attention.items(self)}

    def _statuses_now(self) -> dict[str, str]:
        return {tid: t.status for tid, t in self.st.tickets.items()} | {
            "hook:" + tid: t.status for tid, t in self.st.hooks.items()}

    def _stamp_statuses(self) -> None:
        """A ticket that started waiting on a human is an escalation (the resolver hears)."""
        now = now_iso()
        for tid, t in self.st.tickets.items():
            if self._statuses.get(tid) != t.status:
                t.since = now
                if t.status == Status.WAITING:
                    self.emit(Escalated(self.cid, tid))
        for tid, t in self.st.hooks.items():
            if self._statuses.get("hook:" + tid) != t.status:
                t.since = now
        self._statuses = self._statuses_now()

    def _update_attention(self) -> None:
        cur = {i.key: i for i in attention.items(self)}
        for key in cur.keys() - self._attention.keys():
            item = cur[key]
            self.event("attention", f"raised: {item.title}", item.ticket)
            self.host.attention(item, raised=True)
        for key in cur.keys() & self._attention.keys():   # the same item, enriched
            item = cur[key]
            if item.proposal and item.proposal != self._attention[key].proposal:
                self.event("attention", f"proposal for {item.title}: {item.proposal}", item.ticket)
                self.host.attention(item, raised=True)     # notified again, with it
        for key in self._attention.keys() - cur.keys():
            item = self._attention[key]
            waited = _waited(item.since)
            self.event("attention", f"resolved{f' after {waited}' if waited else ''}: {item.title}",
                       item.ticket)
            self.host.attention(item, raised=False)
        self._attention = cur

    # ------------------------------------------------------------------ A2A cards


    def oracle_commands(self) -> list[str]:
        """The commands that verify the change as a whole: the approved oracle's."""
        orc = Oracle.model_validate(self.pointer_json("oracle") or {})
        return list(dict.fromkeys(orc.acceptance_commands + orc.integration_commands))

    # ------------------------------------------------------------------ events and tasks
    def event(self, kind: str, message: str, ticket: str | None = None,
              run: str | None = None) -> None:
        """Log a notable step, and append it to the change's timeline (a retrospective reads it
        alongside the store's records, which are the history)."""
        log.info("[%s %s] %s%s", self.cid, kind, f"{ticket}: " if ticket else "", message)
        self._append_timeline({"ts": now_iso(), "kind": kind, "ticket": ticket, "run": run,
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

    def spawn(self, coro) -> asyncio.Task:
        """Run a background task (tracked: the controller waits for it, cancels it on stop);
        the controller logs a failure, the event marks the change."""
        return self.host.spawn(coro, on_error=lambda e: self.event("error", f"internal error: {e}"))

    # ------------------------------------------------------------------ documents
    @property
    def workspace(self) -> str:
        """The workspace (repository) the change is made in."""
        return self.change.workspace

    @property
    def git(self) -> GitRemote:
        """The change's workspace's hub."""
        return self.svc.hub(self.change.workspace)

    @property
    def git_url(self) -> str:
        return self.svc.git_url(self.change.workspace)

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
