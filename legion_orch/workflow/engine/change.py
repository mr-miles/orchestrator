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
from ...util import now_iso
from ..model import (
    LIVE_RUN,
    AgentQuestion,
    Change,
    FailureSource,
    Run,
)
from ..results import ActionResult, ReportOutcome
from .base.common import WorkflowError
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

    def start(self, goal: str, workspace: str) -> None:
        """Initialise a new change in ``workspace`` (the caller holds the lock). Its first step
        syncs the workspace and bases the change on its base branch."""
        self.st.change = Change(id=self.cid, goal=goal, workspace=workspace)
        self.write_record("control/goal.md", f"# Goal\n\n{goal}\n\nWorkspace: `{workspace}`\n")
        self._flow.start()
        self.event("change", f"change {self.cid} started in {workspace}")
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
                if not run.consult:        # an A2A turn's asker is gone too: nothing escalates
                    failed.setdefault(run.node_id, run)
        for q in self.change.questions.values():  # their A2A tasks lived in memory
            if q.open:
                self._withdraw(q, "the controller restarted: the asking agent's task is gone")
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

    # ------------------------------------------------------------------ A2A (server/a2a.py)
    def agent_state(self, node_id: str, role: str) -> str | None:
        """A step agent as an A2A recipient: "live", "idle" or None (see RunLifecycle)."""
        return self._lifecycle.agent_state(node_id, role)

    async def a2a_answer(self, node_id: str, role: str, sender: str, text: str) -> str:
        """A step agent, not running, answers ``text`` in one resumed turn."""
        return await self._lifecycle.answer(node_id, role, sender, text)

    def ask_human(self, qid: str, node_id: str, role: str, run: str | None,
                  question: str) -> AgentQuestion:
        """Record an agent's question for a decision owner (the caller holds the lock): it is an
        attention item until answered or withdrawn."""
        q = AgentQuestion(id=qid, node=node_id, role=role, run=run, question=question)
        self.change.questions[qid] = q
        self.event("a2a", f"{node_id} ({role}) asks: {question[:200]}", node_id, run)
        self.save()
        return q

    async def answer_question(self, qid: str, answer: str, approver: str) -> AgentQuestion:
        async with self.lock:
            if approver not in self.cfg.decision_owners:
                raise WorkflowError(f"approver {approver!r} is not a decision owner "
                                    f"({', '.join(self.cfg.decision_owners)})")
            q = self.change.questions.get(qid)
            if q is None:
                raise WorkflowError(f"unknown question {qid}")
            if not q.open:
                raise WorkflowError(f"question {qid} was already "
                                    + ("answered" if q.answer is not None else "withdrawn"))
            if not answer.strip():
                raise WorkflowError("answer is required")
            q.answer, q.answered_by, q.answered_at = answer.strip(), approver, now_iso()
            self.event("a2a", f"{approver} answered {q.node}: {answer.strip()[:200]}", q.node, q.run)
            self.save()
            return q

    def withdraw_question(self, qid: str, reason: str) -> None:
        """The asker can no longer receive the answer (the caller holds the lock)."""
        q = self.change.questions.get(qid)
        if q is not None and q.open:
            self._withdraw(q, reason)
            self.save()

    def _withdraw(self, q: AgentQuestion, reason: str) -> None:
        q.withdrawn = reason
        self.event("a2a", f"question from {q.node} withdrawn: {reason}", q.node, q.run)

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
