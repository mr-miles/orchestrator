"""ChangeEngine: one change's facade – the one public surface of its layers (compose.py).

It is the change's :class:`ChangeCore` (state, documents, events: the :class:`ChangeView`)
and fronts four parts:

* :class:`FlowRuntime` – the workflow: everything that changes it goes through here
* :class:`HumanGates` – what decision owners decide (recorded, then handed to the runtime)
* :class:`Reports` – what agents write and report over MCP (recorded on their runs)
* :class:`RunLifecycle` – run logs, and failing live runs after a restart

It never sees the step layer: control passes only between neighbouring layers.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...store import DocCheck, DocVersion
from ..model import (
    LIVE_RUN,
    Change,
    FailureSource,
    Run,
    SetupPin,
)
from ..results import ActionResult, ReportOutcome
from .base.core import ChangeCore
from .base.shared import EngineHost, Services
from .compose import compose

if TYPE_CHECKING:
    from .runtime.flow_runtime import FlowRuntime
    from .runtime.human import HumanGates
    from .services.reports import Reports
    from .services.runs import RunLifecycle


class ChangeEngine(ChangeCore):
    """Workflow state machine for one change; all durable state goes to the store.

    It is the change's :class:`ChangeCore` plus its collaborators, and the one public surface:
    the controller, the servers and the role handlers (as :class:`RoleContext`) use only the
    methods below. It is given the shared :class:`Services` (the dispatcher among them) and an
    :class:`EngineHost` for the few controller operations it needs (background tasks, run
    ids)."""

    def __init__(self, svc: Services, host: EngineHost, cid: str):
        super().__init__(svc, host, cid)
        self._flow: FlowRuntime
        self._gates: HumanGates
        self._reports: Reports
        self._lifecycle: RunLifecycle
        compose(self)                # its layers (compose.py); it fronts only these four

    # ------------------------------------------------------------------ controller
    def load(self) -> None:
        """Rebuild this change's state from the store (on controller start)."""
        self.st.load()
        self._flow.load()           # the tree, replayed: it orders the nodes (and fills gaps)
        self.seed_attention()

    def start(self, goal: str, base: str, *, blank_repo: bool = False,
              setup_pin: SetupPin | None = None) -> None:
        """Initialise a new change at ``base`` (the caller holds the lock); ``blank_repo``: the
        base has no build tooling or code yet (§5.3b)."""
        self.st.change = Change(id=self.cid, goal=goal, base_sha=base, integration_head=base,
                                blank_repo=blank_repo, setup_pin=setup_pin)
        self.write_record("control/goal.md", f"# Goal\n\n{goal}\n\nBase: `{base}`\n")
        self._flow.start()
        self.event("change", f"change {self.cid} started at {base[:12]}"
                   + (" (a blank repository: the plan starts with a scaffold task)" if blank_repo
                      else ""))
        self.save()

    def recover(self) -> None:
        """After a restart the agent processes are gone, so live runs fail (and their steps
        escalate); every other step's phase takes its work up again (waiting, verifying,
        merging, provisioning, a gate's checks, concluding a turn that ended with the
        controller)."""
        failed = {}
        for run in self.runs.values():
            if run.status in LIVE_RUN:
                self._lifecycle.fail_run(run, "controller restarted while the run was live",
                                         FailureSource.CONTROLLER)
                failed.setdefault(run.node_id, run)
        self._flow.recover(failed)                 # down the layers, as everything else
        self.save()

    # ------------------------------------------------------------------ agents (MCP)
    async def agent_write(self, run: Run, path: str, content: str, base_version: int | None,
                          dry_run: bool = False) -> DocVersion | DocCheck:
        return await self._reports.agent_write(run, path, content, base_version, dry_run)

    def connected(self, run: Run) -> None:
        self._reports.connected(run)

    def progress(self, run: Run, message: str, percent: float | None) -> None:
        self._reports.progress(run, message, percent)

    async def handle_report(self, run_id: str, status: str, summary: str, attestations: list[str],
                            data: dict | None, blocked_reason: str | None,
                            missing_decision: str | None, dry_run: bool = False) -> ReportOutcome:
        return await self._reports.handle_report(run_id, status, summary, attestations, data,
                                                 blocked_reason, missing_decision, dry_run)

    # ------------------------------------------------------------------ operators (web)
    async def human_action(self, node_id: str, action: str, approver: str,
                           payload: dict | None = None) -> ActionResult:
        return await self._gates.human_action(node_id, action, approver, payload)

    async def change_action(self, action: str, approver: str,
                            payload: dict | None = None) -> ActionResult:
        return await self._gates.change_action(action, approver, payload)

    def log_tail(self, run_id: str, lines: int = 200) -> str:
        return self._lifecycle.log_tail(run_id, lines)

    def stored_log(self, run_id: str) -> str:
        return self._lifecycle.stored_log(run_id)

    # ------------------------------------------------------------------ RoleContext
    # ------------------------------------------------------------------ the workflow
    def before_save(self) -> None:
        self._flow.persist()

    @property
    def workflow(self) -> FlowRuntime:
        """The stage rules' state: gates, actions, what waits (read by the API)."""
        return self._flow
