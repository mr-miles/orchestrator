"""ChangeEngine: one change's workflow, composed of collaborators that each own one concern.

* :class:`ChangeCore` – the state, documents and events (the :class:`ChangeView`)
* :class:`StageGraph` – which nodes exist and how they depend on each other
* :class:`RunLifecycle` – failure, timeouts, logs and sandbox deletion of runs
* :class:`Dispatcher` – turning a ready node into a run and starting its agent
* :class:`Acceptance` – verification, the review panel's decision, rework, merge, ship
* :class:`Provisioner` – building the approved sandbox environment
* :class:`Reports` – agent writes, progress and reports
* :class:`HumanGates` – decision owners' actions
* :class:`Scheduler` – the tick that moves the graph forward

Each is given only the parts it uses, so the dependencies are explicit and acyclic
(core <- graph, lifecycle <- dispatcher, acceptance <- reports, gates <- scheduler).
"""
from __future__ import annotations

from ...store import DocCheck, DocVersion
from ..model import (
    LIVE_RUN,
    MERGING,
    RUNNING,
    VERIFYING,
    Change,
    FailureSource,
    Node,
    Run,
    SetupPin,
)
from ..results import ActionResult, ReportOutcome
from .acceptance import Acceptance
from .core import ChangeCore
from .dispatch import Dispatcher
from .graph import StageGraph
from .human import HumanGates
from .phases import PhaseContext
from .provision import Provisioner
from .reports import Reports
from .runs import RunLifecycle
from .runtime import FlowRuntime
from .sandboxes import Sandboxes
from .schedule import Scheduler
from .services import EngineHost, Services
from .steps import StepRunner


class ChangeEngine(ChangeCore):
    """Workflow state machine for one change; all durable state goes to the store.

    It is the change's :class:`ChangeCore` plus its collaborators, and the one public surface:
    the controller, the servers and the role handlers (as :class:`RoleContext`) use only the
    methods below. It is given the shared :class:`Services` and an :class:`EngineHost` for the
    few controller operations it needs (background tasks, run ids, the global parallel limit)."""

    def __init__(self, svc: Services, host: EngineHost, cid: str):
        super().__init__(svc, host, cid)
        self._graph = StageGraph(self)
        self._sandboxes = Sandboxes(self)
        self._lifecycle = RunLifecycle(self, self._sandboxes)
        self._phases = PhaseContext(self, self._sandboxes)
        self._acceptance = Acceptance(self, self._lifecycle, self._phases)
        self._provisioner = Provisioner(self, self._lifecycle, self._phases)
        self._dispatcher = Dispatcher(self, self._lifecycle, self._sandboxes, roles=self)
        self._phases.dispatcher = self._dispatcher
        self._reports = Reports(self, self._lifecycle, roles=self)
        self._gates = HumanGates(self, self._graph, self._acceptance, self._lifecycle)
        self._scheduler = Scheduler(self, self._graph, self._lifecycle, self._dispatcher,
                                    self._acceptance, self._provisioner)
        self._runner = StepRunner(self, self._phases, self._lifecycle, self._acceptance,
                                  self._provisioner, self._scheduler)
        self._acceptance.runner = self._scheduler.runner = self._reports.runner = self._runner
        self._flow = FlowRuntime(self, self._runner, self._lifecycle, self._acceptance)
        self._gates.flow = self._scheduler.flow = self._flow

    # ------------------------------------------------------------------ controller
    def load(self) -> None:
        """Rebuild this change's state from the store (on controller start)."""
        self.st.load()
        self._flow.load()
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
        """After a restart the agent processes are gone, so live runs fail (and escalate);
        interrupted verification, merge and ship steps resume."""
        for run in self.runs.values():
            if run.status in LIVE_RUN:
                self._lifecycle.fail_run(run, "controller restarted while the run was live",
                                         FailureSource.CONTROLLER)
        for node in self.nodes.values():
            if node.status == VERIFYING:
                self.spawn(self._acceptance.verify_node(node.id))
            elif node.status == MERGING:
                self.spawn(self._acceptance.merge_node(node.id))
            elif node.status == RUNNING and (work := self._runner.resume(node)):
                self.spawn(work)                    # provisioning
        for gate in self._flow.evaluating():
            self.spawn(self._flow.evaluate(gate))   # a gate's preconditions
        self._gates.resume_retries()
        self.save()

    async def tick(self) -> None:
        await self._scheduler.tick()

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
    def accept_runs(self, node: Node, roles: set[str] | None = None) -> None:
        self._lifecycle.accept_runs(node, roles)

    def delete_sandbox_later(self, name: str) -> None:
        self._sandboxes.delete_later(name)

    def define_tasks(self, plan: Node) -> list[str]:
        return self._flow.define_tasks(plan)

    # ------------------------------------------------------------------ the workflow
    def sync(self) -> None:
        self._flow.sync()

    @property
    def workflow(self) -> FlowRuntime:
        """The stage rules' state: gates, actions, what waits (read by the API)."""
        return self._flow
