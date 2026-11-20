"""ChangeEngine: one change's workflow, composed of collaborators that each own one concern.

* :class:`ChangeCore` – the state, documents and events (the :class:`ChangeView`)
* :class:`FlowRuntime` – the workflow core's runtime: the stage rules, the nodes' existence and
  order, the gate nodes; the clock's turn enters here (docs/architecture.md §2)
* :class:`StepRunner` – the step layer: each node through its phases, its state, its results
* :class:`Dispatcher`, :class:`Reports`, :class:`Acceptance`, :class:`Provisioner`,
  :class:`RunLifecycle`, :class:`Sandboxes` – the services the phases use
* :class:`HumanGates` – what decision owners decide (recorded, then handed to the runtime)

Control passes only between neighbouring layers; each is given only the parts it uses, so the
dependencies are explicit and acyclic.
"""
from __future__ import annotations

from ...store import DocCheck, DocVersion
from ..model import (
    LIVE_RUN,
    MERGING,
    R_REPORTED,
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
from .human import HumanGates
from .phases import PhaseContext
from .provision import Provisioner
from .reports import Reports
from .runs import RunLifecycle
from .runtime import FlowRuntime
from .sandboxes import Sandboxes
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
        self._sandboxes = Sandboxes(self)
        self._lifecycle = RunLifecycle(self, self._sandboxes)
        self._phases = PhaseContext(self, self._sandboxes)
        self._acceptance = Acceptance(self, self._lifecycle, self._phases)
        self._provisioner = Provisioner(self, self._lifecycle, self._phases)
        self._dispatcher = Dispatcher(self, self._lifecycle, self._sandboxes, roles=self)
        self._phases.dispatcher = self._dispatcher
        self._reports = Reports(self, self._lifecycle, roles=self)
        self._gates = HumanGates(self, self._acceptance, self._lifecycle)
        self._runner = StepRunner(self, self._phases, self._lifecycle, self._acceptance,
                                  self._provisioner)
        self._acceptance.runner = self._reports.runner = self._runner
        self._dispatcher.reports = self._reports
        self._flow = FlowRuntime(self, self._runner, self._lifecycle, self._acceptance)
        self._runner.runtime = self._flow
        self._lifecycle.runner = self._runner
        self._gates.flow = self._flow

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
        """After a restart the agent processes are gone, so live runs fail (and escalate);
        interrupted verification, merge and ship steps resume."""
        for run in self.runs.values():
            if run.status in LIVE_RUN:
                self._lifecycle.fail_run(run, "controller restarted while the run was live",
                                         FailureSource.CONTROLLER)
            elif run.status == R_REPORTED and not run.concluded:
                # its agent's turn ended (with the controller): its report goes to its phase
                self.spawn(self._reports.conclude_later(run.id))
        for node in self.nodes.values():
            if node.status in (VERIFYING, MERGING, RUNNING) \
                    and (work := self._runner.resume_work(node)):
                self.spawn(work)                    # verifying, merging, provisioning
        for gate in self._flow.evaluating():
            self.spawn(self._flow.evaluate(gate))   # a gate's preconditions
        self._runner.resume_retries()
        self.save()

    async def tick(self) -> None:
        """A turn of the controller's clock: it cascades from the workflow runtime down."""
        await self._flow.tick()

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
    def before_save(self) -> None:
        self._flow.persist()

    @property
    def workflow(self) -> FlowRuntime:
        """The stage rules' state: gates, actions, what waits (read by the API)."""
        return self._flow
