"""ChangeCore: one change's state and the record helpers every part of the engine uses.

It implements :class:`~legion_orch.workflow.view.ChangeView` (what governance and ship see) and
is handed to each engine collaborator; it knows nothing of dispatch, reports or verification."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ...store import DocVersion
from .. import attest, governance
from ..model import (
    BUILD,
    ESCALATED,
    Change,
    Escalation,
    FailureSource,
    Node,
    Oracle,
    Resume,
    Run,
)
from .common import log
from .services import EngineHost, Services
from .state import StateStore


class ChangeCore:
    """State (through the :class:`StateStore`), documents and events of one change."""

    def __init__(self, svc: Services, host: EngineHost, cid: str):
        self.svc, self.host, self.cid = svc, host, cid
        self.cfg, self.docs, self.git = svc.cfg, svc.docs, svc.git
        self.assets, self.runner, self.lock = svc.assets, svc.runner, svc.lock
        self.procs = svc.procs
        self.st = StateStore(svc.docs, cid)

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

    def build_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == BUILD]

    def save(self) -> None:
        """Persist the state (the caller holds the lock) and wake the scheduler and UI."""
        self.st.save()
        self.host.changed()

    def escalate(self, node: Node, kind: str, reason: str, resume: Resume, *,
                 run: str | None = None, missing_decision: str | None = None,
                 log_tail: str | None = None, source: FailureSource | None = None) -> None:
        """Stop the node for a human; ``resume`` says where it continues once resolved."""
        node.status = ESCALATED
        node.escalation = Escalation(kind=kind, reason=reason, resume=resume, run=run,
                                     missing_decision=missing_decision, log_tail=log_tail,
                                     source=source)
        self.event("escalation", f"{kind}: {reason}", node.id, run)

    def oracle_commands(self) -> list[str]:
        """The commands that verify the integration head (oracle and surfaces)."""
        orc = self.change.oracle or Oracle()
        cmds = orc.acceptance_commands + orc.integration_commands \
            + governance.surface_commands(self)
        return list(dict.fromkeys(cmds))

    # ------------------------------------------------------------------ events and tasks
    def event(self, kind: str, message: str, node: str | None = None,
              run: str | None = None) -> None:
        """Log a notable step. Not persisted: the store's records (node and run versions,
        control documents) are the history."""
        log.info("[%s %s] %s%s", self.cid, kind, f"{node}: " if node else "", message)
        self.host.touch()

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
