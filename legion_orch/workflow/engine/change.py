"""ChangeEngine: the workflow state machine for one change, composed of concern mixins."""
from __future__ import annotations

import json
import traceback
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ...store import DocVersion
from .. import attest, governance
from ..model import (
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
from .verify import VerifyMixin

if TYPE_CHECKING:
    from .controller import Controller


class ChangeEngine(GraphMixin, DispatchMixin, RunsMixin, ReportsMixin, VerifyMixin, HumanMixin):
    """Workflow state machine for one change; all durable state goes to the store."""

    def __init__(self, ctl: Controller, cid: str):
        self.ctl = ctl
        self.cid = cid
        self.cfg, self.docs, self.git = ctl.cfg, ctl.docs, ctl.git
        self.assets, self.runner, self.lock = ctl.assets, ctl.runner, ctl.lock
        self.procs = ctl.procs
        self.st = StateStore(ctl.docs, cid)

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

    def event(self, kind: str, message: str, node: str | None = None, run: str | None = None,
               data: Any = None) -> None:
        """Log a notable step. Not persisted: the store's records (node and run versions,
        control documents) are the history."""
        log.info("[%s %s] %s%s", self.cid, kind, f"{node}: " if node else "", message)
        self.ctl.version += 1

    def _spawn(self, coro) -> None:
        self.ctl.spawn(coro, on_error=lambda e: self.event(
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

    def snapshot(self) -> dict:
        ch = self.change
        nodes = [n.model_dump(mode="json") | {"feedback_count": len(n.feedback)}
                 for n in self.nodes.values()]
        runs = [r.model_dump(mode="json", exclude={"assignment"}) for r in self.runs.values()]
        return {"project": self.cfg.project, "repo": self.cfg.repo.name,
                "decision_owners": self.cfg.decision_owners, "version": self.ctl.version,
                "assets_sha": self.assets.sha, "limits": self.cfg.limits.__dict__,
                "change": ch.model_dump(mode="json") if ch else None, "nodes": nodes, "runs": runs,
                "gates": governance.gate_set(self) if ch and ch.tier else [],
                "h2": [s.model_dump() for s in governance.h2_status(self)] if ch and ch.requirements else [],
                "surfaces": [s.model_dump() for s in governance.surfaces(self)] if ch else [],
                "attestation_kinds": sorted(governance.ATTESTATION_KINDS)}
