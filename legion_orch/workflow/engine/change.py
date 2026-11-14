"""ChangeEngine: the workflow state machine for one change, composed of concern mixins."""
from __future__ import annotations

import json
import traceback
from typing import Any

from pydantic import BaseModel

from ...store import DocVersion
from .. import attest
from ..model import (
    H_REQUIREMENTS,
    HUMAN,
    INTAKE,
    LIVE_RUN,
    MERGING,
    RUNNING,
    SHIP,
    VERIFYING,
    Change,
    Node,
    Run,
    StateStore,
)
from .common import log
from .dispatch import DispatchMixin
from .graph import GraphMixin
from .human import HumanMixin
from .reports import ReportsMixin
from .runs import RunsMixin
from .services import EngineHost, Services
from .verify import VerifyMixin


class ChangeEngine(GraphMixin, DispatchMixin, RunsMixin, ReportsMixin, VerifyMixin, HumanMixin):
    """Workflow state machine for one change; all durable state goes to the store.

    It is given the shared :class:`Services` and an :class:`EngineHost` for the few controller
    operations it needs (background tasks, run ids, the global parallel limit)."""

    def __init__(self, svc: Services, host: EngineHost, cid: str):
        self.svc, self.host, self.cid = svc, host, cid
        self.cfg, self.docs, self.git = svc.cfg, svc.docs, svc.git
        self.assets, self.runner, self.lock = svc.assets, svc.runner, svc.lock
        self.procs = svc.procs
        self.st = StateStore(svc.docs, cid)

    def load(self) -> None:
        """Rebuild this change's state from the store (on controller start)."""
        self.st.load()

    def start(self, goal: str, base: str) -> None:
        """Initialise a new change at ``base`` (the caller holds the lock)."""
        self.st.change = Change(id=self.cid, goal=goal, base_sha=base, integration_head=base)
        self.write_record("control/goal.md", f"# Goal\n\n{goal}\n\nBase: `{base}`\n")
        self._add(Node(id=INTAKE, kind=INTAKE, title="Intake: requirements & risk"))
        self._add(Node(id=H_REQUIREMENTS, kind=HUMAN, title="Accept requirements & tier",
                       depends_on=[INTAKE]))
        self.event("change", f"change {self.cid} started at {base[:12]}")
        self._save()

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
        return self.svc.git_url

    @property
    def mcp_url(self) -> str:
        return self.svc.mcp_url

    def p(self, rel: str) -> str:
        """Qualify a document path into this change's namespace."""
        return attest.qualify(rel, self.cid)

    def _save(self) -> None:
        self.st.save()
        self.host.changed()

    def event(self, kind: str, message: str, node: str | None = None, run: str | None = None,
               data: Any = None) -> None:
        """Log a notable step. Not persisted: the store's records (node and run versions,
        control documents) are the history."""
        log.info("[%s %s] %s%s", self.cid, kind, f"{node}: " if node else "", message)
        self.host.touch()

    def _spawn(self, coro) -> None:
        self.host.spawn(coro, on_error=lambda e: self.event(
            "error", f"internal error: {e}", data=traceback.format_exc()[-3000:]))

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
