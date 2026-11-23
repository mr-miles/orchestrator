"""ChangeEngine: one change's facade – the one public surface of its layers (compose.py).

It is the change's :class:`ChangeCore` (state, documents, events: the :class:`ChangeView`)
and fronts three parts:

* :class:`TicketFlow` – the workflow (its tickets) and what decision owners decide
* :class:`Reports` – what agents write and report over MCP (recorded on their runs)
* :class:`RunLifecycle` – run logs, and failing live runs after a restart
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...sandbox import GatewaySandbox
from ...store import DocCheck, DocVersion
from ...util import now_iso
from .. import context
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
from .base.common import WorkflowError
from .base.core import ChangeCore
from .base.shared import EngineHost, Services
from .compose import compose
from .services.verification import AGENT, HEAD_CHECK, PUSH
from .services.verification import record_path as verification_path

if TYPE_CHECKING:
    from .flow import Signoffs, TicketFlow
    from .services.agents import AgentRunner, RunTasks
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
        self._flow: TicketFlow
        self._signoffs: Signoffs
        self._reports: Reports
        self._lifecycle: RunLifecycle
        self._verifications: Verifications
        self._tasks: RunTasks
        self._agent_runner: AgentRunner
        self._resolver: Resolver
        compose(self)                # its layers (compose.py); it fronts only these

    # ------------------------------------------------------------------ controller
    def load(self) -> None:
        """Rebuild this change's state from the store (on controller start)."""
        self.st.load()
        self.projections.replay()   # the fields copied from documents (base/projections.py)
        self.st.runs_loaded()       # and the runs, from their documents
        self._flow.load()           # the tickets, checked

        self.seed_attention()

    def start(self, goal: str, workspace: str) -> None:
        """Initialise a new change in ``workspace`` (the caller holds the lock): its skeleton;
        once the workspace is synced (its base), its root starts."""
        self.st.change = Change(id=self.cid, goal=goal, workspace=workspace)
        self._agent_runner.open_change()                     # its orchestrator, its resolver
        self.write_record("control/goal.md", f"# Goal\n\n{goal}\n\nWorkspace: `{workspace}`\n")
        self._flow.start()
        self.event("change", f"change {self.cid} started in {workspace}")
        self.save()

    def adopt_sandboxes(self, found: dict[str, GatewaySandbox], complete: bool) -> None:
        """On start, before :meth:`recover`: the change's sandboxes as the gateway lists them
        (the caller holds the lock)."""
        self._lifecycle.adopt_sandboxes(found, complete)

    def recover(self) -> None:
        """After a restart the agent processes are gone, so live runs end (the controller's
        error: their tickets run them again, at the same attempt – never an attempt of theirs);
        then every ticket under way goes on (R14). The caller holds the lock."""
        for run in self.runs.values():
            if run.status in LIVE_RUN:
                self._lifecycle.fail_run(run, "controller restarted while the run was live",
                                         FailureSource.CONTROLLER)
        self._verifications.recover()             # verifications in flight are gone
        for q in self.change.questions.values():  # their A2A tasks lived in memory
            if q.open:
                self._withdraw(q, "the controller restarted: the asking agent's task is gone")
        self.save()
        self.spawn(self._flow.recover())

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
                            missing_decision: str | None, dry_run: bool = False,
                            outputs: dict | None = None) -> ReportOutcome:
        return await self._reports.handle_report(run_id, status, summary, attestations, data,
                                                 blocked_reason, missing_decision, dry_run,
                                                 outputs)

    # ------------------------------------------------------------------ where a ticket stands
    def ticket_status(self, ticket_id: str) -> dict:
        """A ticket as it is now (workflow/context.py); WorkflowError for an unknown ticket."""
        try:
            return context.ticket_status(self, ticket_id)
        except KeyError:
            raise WorkflowError(f"unknown ticket {ticket_id}") from None

    def workflow_context(self, ticket_id: str) -> dict:
        """How a ticket fits the change (workflow/context.py)."""
        try:
            return context.workflow_context(self, ticket_id)
        except KeyError:
            raise WorkflowError(f"unknown ticket {ticket_id}") from None

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

    def republish_cards(self) -> None:
        """A card catalog subscribed (a restart): the cards the workflow has open, again."""
        self._agent_runner.replay()

    # ------------------------------------------------------------------ verification service
    async def pushed(self, run_id: str, sha: str) -> VerificationRun | None:
        """A run pushed its branch to ``sha`` (the git remote): a build's tip is verified (once
        per sha; a newer push cancels the older tip's that has not started)."""
        async with self.lock:
            run = self.runs.get(run_id)
            t = self.tickets.t.get(run.ticket) if run and self.st.tickets else None
            commands = (t.work.get("verification") if t else None) or []
            if not commands or not run.branch or self.change.status != "ACTIVE":
                return None
            svc = self._verifications
            v = svc.request(svc.key(sha, commands), f"push:{run.id}", PUSH, node=t.id,
                            branch=run.branch)
            self.save()
            return v

    def verification_key_for(self, run: Run, sha: str):
        """The key an agent's request verifies: its ticket's commands at ``sha``."""
        t = self.tickets.t.get(run.ticket)
        commands = (t.work.get("verification") if t else None) or []
        if not commands:
            raise WorkflowError(f"{run.ticket} has no verification commands of its own (only "
                                "builds do)")
        return self._verifications.key(sha, commands)

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
            v = svc.request(key, f"agent:{run.id}", AGENT, node=run.ticket)
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
    def agent_state(self, ticket_id: str, role: str) -> str | None:
        """A ticket's agent as an A2A recipient: "live", "idle" or None (see RunLifecycle)."""
        return self._lifecycle.agent_state(ticket_id, role)

    def ask_human(self, qid: str, ticket_id: str, role: str, run: str | None,
                  question: str) -> AgentQuestion:
        """Record an agent's question for a decision owner (the caller holds the lock): it is an
        attention item until answered or withdrawn."""
        q = AgentQuestion(id=qid, ticket=ticket_id, role=role, run=run, question=question)
        self.change.questions[qid] = q
        self.event("a2a", f"{ticket_id} ({role}) asks: {question[:200]}", ticket_id, run)
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
            self.event("a2a", f"{approver} answered {q.ticket}: {answer.strip()[:200]}", q.ticket,
                       q.run)
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
        self.event("a2a", f"question from {q.ticket} withdrawn: {reason}", q.ticket, q.run)

    # ------------------------------------------------------------------ operators (web)
    async def human_action(self, ticket_id: str, action: str, approver: str,
                           payload: dict | None = None) -> ActionResult:
        """A decision owner's action on a ticket (a sign-off's decision, a remedy)."""
        note = await self._flow.act(ticket_id, action, approver, payload)
        t = self.tickets.t.get(ticket_id)
        return ActionResult(note=note, status=str(t.status) if t else None)

    async def change_action(self, action: str, approver: str,
                            payload: dict | None = None) -> ActionResult:
        """A decision owner's action on the change: pause, resume, abandon, replan."""
        return ActionResult(note=await self._flow.change_action(action, approver, payload))

    def signoffs_waiting(self) -> dict[str, dict]:
        """The sign-offs waiting for a decision owner: ticket -> what it shows (its subjects)."""
        return {tid: {"subjects": subjects, "attempt": work.attempt}
                for tid, (work, subjects, _) in self._signoffs.waiting.items()}

    def log_tail(self, run_id: str, lines: int = 200) -> str:
        return self._lifecycle.log_tail(run_id, lines)

    def stored_log(self, run_id: str) -> str:
        return self._lifecycle.stored_log(run_id)

    # ------------------------------------------------------------------ the workflow
    def before_save(self) -> None:
        self._flow.housekeeping()

    @property
    def workflow(self) -> TicketFlow:
        """The change's workflow: its tickets and their runner (read by the API)."""
        return self._flow
