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
from .. import cards, context
from ..model import (
    LIVE_RUN,
    V_FINAL,
    V_LIVE,
    AgentQuestion,
    Change,
    FailureSource,
    Run,
    VerificationRun,
)
from ..results import ActionResult, ReportOutcome
from .base.common import NoAgent, WorkflowError
from .base.core import ChangeCore
from .base.shared import EngineHost, Services
from .compose import compose
from .services.verification import AGENT, HEAD_CHECK, PUSH
from .services.verification import record_path as verification_path

if TYPE_CHECKING:
    from .runtime.flow_runtime import FlowRuntime
    from .runtime.human import HumanGates
    from .services.inbox import Inboxes, RunTasks
    from .services.reports import Reports
    from .services.resolver import Resolver
    from .services.runs import RunLifecycle
    from .services.verification import Verifications


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
        self._verifications: Verifications
        self._tasks: RunTasks
        self._inboxes: Inboxes
        self._resolver: Resolver
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
        self.publish_cards(cards.change_cards(self.cid))     # its orchestrator, its resolver
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
        self._verifications.recover()             # verifications in flight are gone
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

    # ------------------------------------------------------------------ the resolver
    def escalated(self, node_id: str) -> None:
        """A step escalated (ChangeCore, on a save): its resolver is asked, in the background."""
        if self.cfg.resolver.enabled:
            self.host.spawn(self._resolver.resolve(node_id))

    # ------------------------------------------------------------------ where a step stands
    def step_status(self, node_id: str) -> dict:
        """A step as it is now (workflow/context.py); WorkflowError for an unknown step."""
        try:
            return context.step_status(self, node_id)
        except KeyError:
            raise WorkflowError(f"unknown step {node_id}") from None

    def workflow_context(self, node_id: str) -> dict:
        """How a step fits the change (workflow/context.py)."""
        try:
            return context.workflow_context(self, node_id, self._flow.stages())
        except KeyError:
            raise WorkflowError(f"unknown step {node_id}") from None

    # ------------------------------------------------------------------ inboxes (services/inbox.py)
    def task_view(self, run: Run) -> dict:
        """The run's task as the agent's inbox shows it."""
        return self._tasks.view(run)

    async def reply_task(self, run: Run, state: str, text: str,
                         unanswered: list[tuple[str, str]] = ()) -> dict:
        """The agent's a2a_reply on its run's task: ends it, or is answered with what is
        missing (an ask-again; the messages still waiting in its inbox, each once)."""
        async with self.lock:
            return self._tasks.reply(run, state, text, list(unanswered))

    def inbox(self, run: Run, assignment: dict) -> list[dict]:
        """Everything waiting on ``run``'s agent: its work, messages, questions back."""
        return self._inboxes.view(run, assignment)

    async def a2a_reply(self, run: Run, task_id: str, text: str, state: str) -> dict:
        """The agent's a2a_reply on any task in its inbox; returns what is left in it."""
        return await self._inboxes.reply_from(run, task_id, text, state)

    async def deliver(self, card_id: str, sender: str, sender_role: str, text: str,
                      task_id: str) -> tuple[str, str]:
        """A message to a card's agent (the A2A layer carries it): its reply (state, text)."""
        card = next((c for c in self.a2a_cards() if c.id == card_id), None)
        if card is None or card.node is None:
            raise NoAgent(f"{card_id} is not an agent of this change")
        return await self._inboxes.deliver(card, sender, sender_role, text, task_id)

    def pending_messages(self, card_id: str) -> list:
        """Messages waiting in a card's inbox (for views and tests)."""
        return self._inboxes.pending(card_id)

    def questions_back(self, card_id: str) -> list:
        return self._inboxes.questions_for(card_id)

    # ------------------------------------------------------------------ verification service
    async def pushed(self, run_id: str, sha: str) -> VerificationRun | None:
        """A run pushed its branch to ``sha`` (the git remote): a build task's tip is verified
        (once per sha; a newer push cancels the older tip's that has not started)."""
        async with self.lock:
            run = self.runs.get(run_id)
            node = self.nodes.get(run.node_id) if run else None
            if node is None or not node.task or not run.branch or self.change.status != "ACTIVE":
                return None
            svc = self._verifications
            v = svc.request(svc.key(sha, node.task.verification), f"push:{run.id}", PUSH,
                            node=node.id, branch=run.branch)
            self.save()
            return v

    def verification_key_for(self, run: Run, sha: str):
        """The key an agent's request verifies: its step's task commands at ``sha``."""
        node = self.nodes.get(run.node_id)
        if node is None or not node.task:
            raise WorkflowError(f"{run.node_id} has no verification commands of its own (only "
                                "build tasks do)")
        return self._verifications.key(sha, node.task.verification)

    async def request_verification(self, run: Run, sha: str) -> VerificationRun:
        """An agent asks for its step's commands to be verified at ``sha`` (MCP): its result if
        verified already, else queued (lowest priority), within the run's request limit."""
        async with self.lock:
            key = self.verification_key_for(run, sha)
            svc = self._verifications
            known = svc.latest(key)
            if not (known and (known.state in V_FINAL or known.state in V_LIVE)):
                limit = self.cfg.limits.verification_requests_per_run
                if run.verification_requests >= limit:
                    raise WorkflowError(f"this run asked for {limit} verifications already "
                                        "(limits.verification_requests_per_run)")
                run.verification_requests += 1
            v = svc.request(key, f"agent:{run.id}", AGENT, node=run.node_id)
            self.save()
            return v

    def latest_verification(self, run: Run, sha: str) -> VerificationRun | None:
        """The latest verification of ``run``'s step commands at ``sha`` (as the key is now)."""
        return self._verifications.latest(self.verification_key_for(run, sha))

    def verifications(self, sha: str | None = None) -> list[VerificationRun]:
        runs = list(self.change.verifications.values())
        return [r for r in runs if r.sha == sha] if sha else runs

    def verification_record(self, vid: str) -> dict | None:
        """A finished verification's whole record (command output included)."""
        path = verification_path(vid)
        return self.json_doc(path) if self.latest(path) else None

    async def reverify(self, vid: str, approver: str) -> VerificationRun:
        """A decision owner runs a verification again (a new attempt): a flaky result is
        challenged, the attempts stay visible."""
        async with self.lock:
            if approver not in self.cfg.decision_owners:
                raise WorkflowError(f"approver {approver!r} is not a decision owner "
                                    f"({', '.join(self.cfg.decision_owners)})")
            old = self.change.verifications.get(vid)
            if old is None:
                raise WorkflowError(f"unknown verification {vid}")
            svc = self._verifications
            v = svc.request(svc.key(old.sha, old.commands), f"operator:{approver}", HEAD_CHECK,
                            node=old.node, force=True)
            self.save()
            return v

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
