"""Agent runs: the factory of an agent's :class:`.Work` for a ticket's round – the run record
(guards G-D2, G-D4), its prompt and payload, its sandbox session and commands (the working copy's
preparation, then the agent's turn, :class:`AgentTurn`) – and how the run ended, for its ticket
(:class:`.AgentEnd`): its accepted report's outcome, or its failure and who it came from. A
ticket's agent writes only in the ticket's directory (``tickets/<id>/``) and reports the outputs
its role names; it is given its inputs (the approved versions it consumes), its work and the
findings its last round was told to fix."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ....bus import AgentStarted, TurnEnded
from ....config import agent_command
from ....sandbox import (
    AGENT_PREFIX,
    agentlog,
    write_payload,
)
from ....util import now_iso
from ... import attest, cards
from ...assets import AssetError, GitMode
from ...model import (
    LIVE_RUN,
    R_ACCEPTED,
    R_FAILED,
    R_REPORTED,
    CommandResult,
    FailureSource,
    Run,
    RunStage,
)
from ...roles import handler_for
from ...roles.base import outputs_of_owner
from ...tickets import Status, Ticket
from ...tickets.executors import AgentEnd
from ...tickets.executors import Work as TicketWork
from ...view import RoleContext
from ..base.common import (
    SKILLS_DIR,
    AgentBusy,
    NoAgent,
    WorkflowError,
    model_env,
    write_skills,
)
from ..base.core import ChangeCore
from ..base.work import AGENT, Command, Outcome, Session, Work
from ..sandboxes.plan import Checkout, SandboxPlan
from ..sandboxes.sandboxes import AgentSession, Sandboxes
from .agents.placement import Placement
from .runs import RunLifecycle

if TYPE_CHECKING:
    from .agents.runner import AgentRunner
    from .agents.tasks import RunTasks
    from .reports import Reports


@dataclass
class Assignment:
    """What a run is told: template variables, its write scope, the outputs it must report,
    its context attestations, its work and what its last round was told to fix."""
    variables: dict[str, Any]
    write_scope: list[str]
    outputs: list[str]
    context: list[str]
    task: dict[str, Any]
    findings: list[str]

    def for_agent(self) -> dict[str, Any]:
        """The assignment returned by get_assignment() (the rendered list of context refs is
        replaced by the list itself)."""
        return {k: v for k, v in self.variables.items() if k != "context_refs"} | {
            "work": self.task, "context": self.context, "outputs": self.outputs,
            "findings": self.findings}


def _file_path(ref: str) -> str | None:
    """The document path of a ``file:<path>@<version>`` attestation (None for git refs)."""
    return ref[5:].rsplit("@", 1)[0] if ref.startswith("file:") and "@" in ref else None


def assets_record(sha: str) -> str:
    return f"control/assets/{sha.removeprefix('sha256:')[:16]}.json"


@dataclass
class Prepared:
    """What an agent's work prepared in its slot: its run, or why it could not run."""
    run: Run | None = None
    refused: str | None = None


def ticket_dir(ticket_id: str) -> str:
    """A ticket's directory in the change: everything its work writes (``/`` is not in an id)."""
    return f"tickets/{ticket_id}"


START_PROMPT = """You are the `{role}` agent of ticket `{node}` in change {change}.

Your work is a task in your inbox: call `inbox()` (legion MCP) first – task `{task}`, from the
orchestrator, holds your assignment; messages from other agents of the change arrive there too.
Load the `legion-harness-contract` skill before your first action. File your results with
`report(...)` – accepted, it ends the task – then end your turn.
"""

CONTINUE_PROMPT = """The orchestrator, on your task `{task}`:

{message}

Your inbox (`inbox()`) has the task and any messages for you.
"""

ANSWER_PROMPT = """# A message while you were not running

You are the `{role}` agent of ticket `{node}` in change {change}. `{sender}` – another agent of this
change – sent you this message while you were not running:

<message from="{sender}">
{text}
</message>

Answer it from what you know of your work in this session: reply with
`a2a_reply(task_id="{task}", text=...)` (or end with your answer as your final message). Your
tools in this turn are the Skill tool and your legion MCP tools (read the documents, your
inbox): nothing is to change – no files, no reports (the oracle alone may confirm that a test of
its is wrong, with `test_defect`). If you cannot answer, say so and why.
"""

RESOLVER_PROMPT = """# A message for the resolver

From `{sender}`:

<message from="{sender}">
{text}
</message>

Answer it – `a2a_reply(task_id="{task}", text=...)`, or your final message (an escalation: end
with your proposal, as your instructions say). Investigate with your tools and ask other agents
over A2A; change nothing.

Your instructions:

{prompt}
"""

@dataclass
class AgentTurn(Command):
    """The agent's turns: start the agent, then the agent runner takes its turns
    (:meth:`AgentRunner.take_turns`: another while the run's task is open, each awaited under
    the run's timers). Then the run's record is completed (exit code, a live run failed with
    why, the log archived)."""
    runs: AgentRuns | None = None
    of: Run | None = None                   # the run whose agent this is

    async def run(self, session: Session) -> CommandResult:
        runs, run = self.runs, self.of
        c = runs.c
        proc = await runs.sandboxes.start_agent(run.sandbox, c.run_dir(run.id) / "agent.log")
        async with c.lock:
            c.procs[run.id] = proc
            c.event("sandbox", f"agent process started in sandbox {run.sandbox}",
                    run.ticket, run.id)
            c.save()
        rc = await runs.runner.take_turns(run, proc)
        async with c.lock:
            c.procs.pop(run.id, None)
            run.exit_code = rc
            if run.consult:                 # an A2A turn: its answer is its final message
                run.status, run.ended_at = (R_ACCEPTED if rc == 0 else R_FAILED), now_iso()
                c.event("a2a", f"consult turn exited ({rc})", run.ticket, run.id)
            elif run.status in LIVE_RUN:
                reason, source = runs._exit_reason(run, rc)
                runs.lifecycle.fail_run(run, reason, source, exit_code=rc)
            else:
                c.event("agent", f"agent process exited ({rc})", run.ticket, run.id)
            run.stage = RunStage.EXITED
            runs.lifecycle.archive_log(run)
            if not run.consult:              # what it left in its inbox gets turns of its own
                c.emit(TurnEnded(c.cid, run.ticket, run.role))
            c.save()
        return self.result(rc, "")


class AgentRuns:
    """Makes an agent's work for a ticket's round, and reads what it did."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, sandboxes: Sandboxes,
                 roles: RoleContext):
        self.c, self.lifecycle, self.sandboxes, self.roles = core, lifecycle, sandboxes, roles
        self.reports: Reports = None  # type: ignore[assignment]   # set once it exists
        self.runner: AgentRunner = None  # type: ignore[assignment]  # its turns (compose)
        self.tasks: RunTasks | None = None                   # set once it exists
        self.placement = Placement(core, sandboxes, lifecycle)   # where each agent works

    # ------------------------------------------------------------------ the run's task
    def ask_again_after(self, run: Run, rc: int) -> str | None:
        """Under the lock, after a turn of ``run`` ended (exit ``rc``): the orchestrator's
        message for another turn, or None – the task is over, or the exit was a failure (the
        caller fails it as before), or no attempt is left. Asks used up with an accepted report
        leave the report's word as the reply (it is the record that matters)."""
        tasks = self.tasks
        if run.consult or tasks is None or tasks.ended(run) or rc != 0:
            return None
        if run.status not in LIVE_RUN:                  # its report ended its task
            return None
        # only an agent that ended its turn is asked again: a CLI that crashed, failed or exited
        # waiting on its background tasks fails the run as before (_exit_reason)
        result = agentlog.agent_result(self.lifecycle.log_tail(run.id, 2000))
        if result is None or result.is_error or result.pending:
            return None
        return tasks.ask_again(run)

    async def continue_turn(self, run: Run, message: str, turn: int):
        """Resume ``run``'s agent session for another turn on ``message`` (its full tools and
        MCP, as its first turn), its log appended to the run's."""
        sid = run.session_id or agentlog.session_id(self.lifecycle.log_tail(run.id, 100_000))
        env = self._env(run) | {"LEGION_NEEDS_REPO": "0"} | (
            {"LEGION_RESUME_SESSION": sid} if sid else {})
        d = write_payload(self.c.run_dir(f"{run.id}-turn{turn}") / "payload", env,
                          prompt=CONTINUE_PROMPT.format(task=run.id, message=message),
                          soul=self.c.assets.soul_text(run.soul), mcp=self._mcp_json())
        await self.sandboxes.upload(run.sandbox, d)
        return await self.sandboxes.start_agent(run.sandbox, self.c.run_dir(run.id) / "agent.log")

    def _mcp_json(self) -> str:
        # No credential: the OpenShell gateway injects the bearer for the controller endpoint.
        # With the local runner the bearer is in the process env (gateway emulation) and Claude
        # Code expands ${LEGION_ACCESS_TOKEN} in the MCP config at start-up.
        # alwaysLoad: the controller's tools load at start, not deferred behind a tool search
        server = {"type": "http", "url": self.c.mcp_url, "alwaysLoad": True}
        if self.c.cfg.identity.dev_spire:
            server["headers"] = {"Authorization": "Bearer ${LEGION_ACCESS_TOKEN}"}
        return json.dumps({"mcpServers": {"legion": server}}, indent=2)

    def _env(self, run: Run, *, tools: bool = True) -> dict[str, str]:
        env = {
            "LEGION_RUN_ID": run.id, "LEGION_ROLE": run.role,
            "LEGION_GIT_URL": self.c.git_url, "LEGION_MCP_URL": self.c.mcp_url,
            "LEGION_AGENT_COMMAND": agent_command(self.c.cfg.sandbox, tools=tools),
        } | model_env(run)
        # A2A (server/a2a.py): run-agent.sh registers the change's AI Catalog with the a2a CLI;
        # its default agent is the change's orchestrator
        a2a = self.c.cfg.server.a2a_url
        orchestrator = cards.card_url(a2a, cards.ORCHESTRATOR)
        return env | {"LEGION_A2A_CATALOG": cards.catalog_url(a2a),
                      "LEGION_A2A_ORCHESTRATOR": orchestrator,
                      "LEGION_A2A_CARD": cards.card_id(run.role, run.ticket),
                      "A2ACLI_AGENT_CARD": orchestrator}

    # ------------------------------------------------------------------ A2A: an early start
    async def consult(self, ticket_id: str, role: str, sender: str, text: str,
                      task_id: str | None = None) -> str:
        """A message for a ticket's agent that is not running: one turn of its own that answers
        – the one way an agent takes a turn outside its ticket's work (a consult run: not one of
        the ticket's runs, read-only, never reporting).

        Only an agent that has started answers (one that has not has no card): its sandbox –
        stopped or not – is resumed **in place**, its session continued, with only the Skill tool
        and its MCP tools (it may not touch its working copy), under the sandbox's lock for the
        whole turn: a rework launched meanwhile waits. The resolver answers in its own sandbox
        with all its tools.

        It replies with ``a2a_reply`` on ``task_id``; failing that, its final message is the
        answer (returned). Afterwards the sandbox is stopped, not deleted."""
        answerer = self.sandboxes.answerer
        async with answerer.lock(ticket_id, role):
            if answerer.live(ticket_id, role):
                raise AgentBusy(f"{role}@{ticket_id} is running")
            async with self.c.lock:
                run, plan, in_place = self._consult_run(ticket_id, role, sender, text, task_id)
                self.c.event("a2a", (f"{role}@{ticket_id} is not running: resuming it in "
                                     f"{run.sandbox}" if in_place else
                                     f"{role}@{ticket_id} has not started: starting it in "
                                     f"{run.sandbox}") + f" (run {run.id}) to answer {sender}",
                             ticket_id if ticket_id in self.c.tickets.t else None, run.id)
                self.c.save()
            if in_place:
                async with self.sandboxes.lock(run.sandbox):
                    return await self._consult_turn(run, plan, sender, in_place=True)
            return await self._consult_turn(run, plan, sender, in_place=False)

    async def _consult_turn(self, run: Run, plan: SandboxPlan, sender: str, *,
                            in_place: bool) -> str:
        fresh = not any(r.sandbox == run.sandbox and r.id != run.id for r in self.c.runs.values())
        checkout = [] if in_place else await self.sandboxes.checkout(run, fresh)
        work = Work(session=AgentSession(self.sandboxes, run, plan, fresh,
                                         lambda stage, msg: self._stage(run, stage, msg),
                                         held=in_place),
                    commands=[*checkout,
                              AgentTurn("the agent's turn", AGENT, True, runs=self, of=run)])
        work.log = lambda cmd, r: self._log(run, cmd, r)
        out = await work.execute()
        async with self.c.lock:
            if run.status in LIVE_RUN:          # never got to its turn
                run.status, run.ended_at = R_FAILED, now_iso()
            self.sandboxes.suspend_when_idle({run.sandbox})     # stopped, not deleted
            self.c.save()
        result = agentlog.agent_result(self.lifecycle.stored_log(run.id))
        if out.error or result is None or result.is_error or not result.message.strip():
            raise WorkflowError(f"{run.role}@{run.ticket} could not answer: "
                                + (out.error or f"no answer (exit {run.exit_code})"))
        async with self.c.lock:
            self.c.event("a2a", f"{run.role}@{run.ticket} answered {sender}",
                         run.ticket if run.ticket in self.c.tickets.t else None, run.id)
            self.c.save()
        return result.message.strip()

    def _consult_run(self, ticket_id: str, role: str, sender: str, text: str,
                     task_id: str | None) -> tuple[Run, SandboxPlan, bool]:
        """The record of a consult (under the lock): the role's run as it would be now – in the
        agent's sandbox if it has one (in place; the resolver: its own), else a new one – not one
        of the ticket's runs, with nothing to write and nothing to report."""
        ticket, ch = self._ticket_of(ticket_id), self.c.change
        pushes = self.c.assets.roles[role].git == GitMode.COMMIT
        plan = SandboxPlan(Checkout.BRANCH if pushes else Checkout.BASE)
        tw = self.c.round_of(ticket)                         # its round as it would start now
        asg = self._assignment(ticket, role, tw, None if pushes else tw.base)
        try:
            rendered = self.c.assets.render_role(role, asg.variables)
        except AssetError as e:
            raise WorkflowError(f"{role}@{ticket_id}: {e}") from None
        kept = self.sandboxes.answerer.session_run(ticket_id, role)   # its sandbox, if it has one
        if kept is None and role != cards.RESOLVER:      # only agents that have started answer
            raise NoAgent(f"{role}@{ticket_id} has not started: a message waits until it has")
        if kept is not None and kept.sandbox in self.sandboxes.retiring:
            raise NoAgent(f"{role}@{ticket_id} finished its work (its sandbox is being retired)")
        in_place = kept is not None and role != cards.RESOLVER
        run = Run(id=self.c.host.next_run_id(), ticket=ticket.id, role=role,
                  attempt=ticket.attempt, soul=self._soul(ticket, role),
                  base_sha=tw.base or ch.base_sha, judged_sha=None if pushes else tw.base,
                  prompt_pin=rendered.pin, assets_sha=self.c.assets.sha,
                  assignment=asg.for_agent(), context_refs=asg.context, consult=True,
                  sandbox=kept.sandbox if kept else self.c.host.new_sandbox_name(AGENT_PREFIX),
                  sandbox_ref=kept.sandbox_ref if kept else None,
                  **self._model(role, self._soul(ticket, role), kept))
        self.c.runs[run.id] = run
        template = RESOLVER_PROMPT if role == cards.RESOLVER else ANSWER_PROMPT
        prompt = template.format(role=role, node=ticket_id, change=ch.id, sender=sender,
                                 text=text, prompt=rendered.prompt,
                                 task=task_id or "(none: answer in your final message)")
        self._payload(run, dataclasses.replace(rendered, prompt=prompt), plan,
                      tools=not in_place)            # in place: it may not touch its working copy
        return run, plan, in_place

    def _ticket_of(self, ticket_id: str) -> Ticket:
        """A ticket, or – the change's resolver, no ticket's – a stand-in to render its
        assignment against."""
        if (ticket := self.c.ticket(ticket_id)) is not None:
            return ticket
        if ticket_id == cards.RESOLVER:
            return Ticket(id=cards.RESOLVER, type=f"agent:{cards.RESOLVER}", goal="Resolver")
        raise WorkflowError(f"unknown ticket {ticket_id}")

    def _model(self, role: str, soul: str, kept: Run | None = None) -> dict[str, str | None]:
        """The model and effort a run asks for: its role's and soul's (config ``models``) – a
        turn in a kept session, its session's, which a model change would not continue."""
        if kept is not None and kept.role == role:
            return {"model": kept.model, "effort": kept.effort}
        m = self.c.cfg.models.resolve(role, soul)
        return {"model": m.model, "effort": m.effort}

    def _soul(self, ticket: Ticket, role: str) -> str:
        """The role's soul – an implementer's, its work's (chosen by the planner)."""
        return ticket.work.get("soul") if role == "implementer" and ticket.work.get("soul") \
            else self.c.assets.roles[role].soul

    # ------------------------------------------------------------------ the work
    def work(self, ticket: Ticket, role: str, tw: TicketWork, plan: SandboxPlan, *,
             wanted: Callable[[], bool], started: Callable[[Run], None]) -> tuple[Work, Prepared]:
        """The agent's work for the ticket's round ``tw``. It is prepared in its slot, under the
        lock: unless the ticket no longer ``wanted`` it, the run record is made (or its prompt
        does not render) and ``started`` is told; then its sandbox session and commands."""
        prepared = Prepared()

        async def prepare(work: Work) -> bool:
            async with self.c.lock:
                if not wanted():
                    return False
                run = self.prepare(ticket, role, tw, plan)
                if isinstance(run, str):
                    prepared.refused = run
                    return False
                prepared.run = run
                started(run)
                self.c.save()
            fresh = not any(r.sandbox == run.sandbox and r.id != run.id
                            for r in self.c.runs.values())
            work.session = AgentSession(self.sandboxes, run, plan, fresh,
                                        lambda stage, msg: self._stage(run, stage, msg))
            work.commands = [*await self.sandboxes.checkout(run, fresh),
                             AgentTurn("the agent's turn", AGENT, True, runs=self, of=run)]
            work.log = lambda cmd, r: self._log(run, cmd, r)
            return True
        # it starts once its role's environment is built (no sandbox from the old build)
        return Work(prepare=prepare, ready=lambda: self.sandboxes.environment_built(role)), prepared

    def finish(self, run: Run, outcome: Outcome) -> None:
        """Under the lock, however the work ended (also when its ticket no longer wants the
        result): a run whose sandbox could not be made ready fails."""
        if run.status not in LIVE_RUN:
            return
        if outcome.error:
            why = outcome.error
        elif (stop := outcome.stopped) and stop.command.kind != AGENT:
            why = f"{stop.command.label} failed (exit {stop.result.exit_code}): " \
                  f"{stop.result.output[-800:]}"
        else:
            return
        self.lifecycle.fail_run(run, f"sandbox launch failed: {why}", FailureSource.SYSTEM)

    async def conclude(self, run: Run) -> AgentEnd | None:
        """Under the lock: how the run ended, for its ticket – its accepted report's outcome
        (its role handler's say), or its failure (who it came from); None: not over yet."""
        if run.status == R_REPORTED:
            return await self.reports.conclude(run)
        if run.failure:
            f = run.failure
            return AgentEnd(False, failure=f.source, detail=self.lifecycle.why_failed(run))
        return None

    def _log(self, run: Run, cmd: Command, r: CommandResult) -> None:
        """The preparation's steps go to the run's agent log."""
        if cmd.kind == AGENT:
            return
        with open(self.c.run_dir(run.id) / "agent.log", "a") as log:
            log.write(f"[legion] {cmd.label}: exit {r.exit_code}\n{r.output}")
            if r.exit_code and not cmd.required:
                log.write(f"[legion] {cmd.label} failed (exit {r.exit_code}): the working copy "
                          "may lack its dependencies\n")

    # ------------------------------------------------------------------ the run record
    def _context_refs(self, ticket: Ticket, role: str, tw: TicketWork) -> list[str]:
        """What the run is given to read: its inputs (the approved versions it consumes), a
        judge's the outputs of the work it judges (its owner's), what its work declares it
        needs, and its role handler's own (workflow/roles/)."""
        refs: list[str] = []
        for value in tw.inputs.values():
            refs += value if isinstance(value, list) else [value] if value else []
        if ticket.role == "criterion":
            refs += outputs_of_owner(self.roles, ticket)
        given = {_file_path(a) for a in refs} - {None}     # at the version the controller gives
        for x in (*ticket.work.get("context", []), *handler_for(role).extra_context(self.roles,
                                                                                   ticket)):
            a = attest.normalize(x, self.c.cid)
            if _file_path(a) not in given:
                refs.append(a)
        return list(dict.fromkeys(refs))

    def _oracle_sha(self) -> str:
        """The protected acceptance tests' commit: the latest oracle instance's to pass (H-2) –
        a fix's, once there is one."""
        return next((t.sha for t in reversed(list(self.c.tickets.t.values()))
                     if t.type == "agent:oracle" and t.status == Status.PASSED and t.sha), "")

    @staticmethod
    def _findings_text(tw: TicketWork) -> str:
        """What its last round was told to fix (a failed criterion's findings)."""
        return "\n".join(f"- {f}" for f in tw.findings) or "(none)"

    def prepare(self, ticket: Ticket, role: str, tw: TicketWork,
                plan: SandboxPlan) -> Run | str:
        """Create a run record under the lock (guards G-D1, G-D2, G-D4), or say why it cannot
        run (its prompt does not render)."""
        ch = self.c.change
        git = self.c.assets.roles[role].git
        # what it works at and attests: a judge, the commit it judges; a role that works at a
        # commit it does not make (integration, describer), its base
        judged = tw.judged if plan.checkout == Checkout.CANDIDATE \
            else tw.base if git == GitMode.HEAD else None
        branch = tw.branch if plan.pushes else None
        asg = self._assignment(ticket, role, tw, judged)
        try:
            rendered = self.c.assets.render_role(role, asg.variables)
        except AssetError as e:
            return str(e)
        run = Run(id=self.c.host.next_run_id(), ticket=ticket.id, role=role,
                  attempt=tw.attempt, soul=self._soul(ticket, role),
                  base_sha=tw.base or ch.base_sha, branch=branch, judged_sha=judged,
                  prompt_pin=rendered.pin, assets_sha=self.c.assets.sha,
                  template_vars={k: str(v) for k, v in asg.variables.items()},
                  write_scope=asg.write_scope, outputs=asg.outputs,
                  context_refs=asg.context, assignment=asg.for_agent(),
                  **self._model(role, self._soul(ticket, role)))
        self._record_assets()
        owner = self.c.tickets.t.get(ticket.parent) if ticket.role == "criterion" else None
        self.placement.place(run, ticket, plan, judged=owner)
        self.c.runs[run.id] = run
        ticket.runs.append(run.id)
        self.c.emit(AgentStarted(self.c.cid, ticket.id, role, run.sandbox))
        if plan.keeps and ticket.sandbox is None:
            ticket.sandbox = run.sandbox
        self._payload(run, rendered, plan)
        self.c.event("dispatch", f"{role} run {run.id} (attempt {run.attempt}, soul {run.soul}, "
                     f"pin {rendered.pin[7:19]})", ticket.id, run.id)
        return run

    def _sandbox_tools(self, role: str) -> str:
        """What the role's sandbox has for the project, stated as a fact: its environment
        profile's tools, or – without a provisioned environment – the default image, to check."""
        ch = self.c.change
        env = ch.environment.roles.get(role) if ch.environment else None
        tools: list[str] = []
        if env is not None and (spec := self.c.pointer_json("environment")) is not None:
            try:
                tools = list(spec["profiles"][env.profile]["tools"])
            except (KeyError, TypeError):
                tools = []
        if not tools:
            return ("the operator's default image: check a tool with `command -v` before relying "
                    "on it – do not assume python3, node or rsync; verification runs in the "
                    "verifier image, which may have less")
        return (f"profile `{env.profile}`: {', '.join(tools)}. Build, test and verify only with "
                "these (the verifier has them too); anything else on PATH, such as python3, is "
                "at most for your own edits, never for a verification command")

    def _assignment(self, ticket: Ticket, role: str, tw: TicketWork,
                    judged_sha: str | None) -> Assignment:
        """The template variables, write scope, required outputs and context of a run (guard
        G-D2 binds them). A ticket writes only in its own directory."""
        ch, work = self.c.change, ticket.work
        if not work and ticket.role == "criterion" and ticket.parent:   # a judge: the work's
            work = self.c.tickets.t[ticket.parent].work                 # contract it judges
        own = ticket_dir(ticket.id)
        outputs = list(self.c.assets.roles[role].outputs)     # ``name*``: a list
        variables = {
            "change_id": ch.id, "goal": ch.goal, "tier": ch.tier or "unassessed",
            "repo_name": self.c.workspace,
            "base_sha": tw.base or ch.base_sha or "", "head_sha": tw.base or "",
            "change_base_sha": ch.base_sha or "",             # the change's whole diff from here
            "oracle_sha": self._oracle_sha(),
            "branch": tw.branch if tw.branch and self.c.assets.roles[role].git == GitMode.COMMIT
            else "(none: a detached checkout of the commit you work at)",
            "ticket_id": ticket.id, "ticket_goal": ticket.goal or ticket.id,
            "work_json": json.dumps(work, indent=2),
            "attempt": tw.attempt, "role": role, "judged_sha": judged_sha or "",
            "workdir": f"{self.c.cfg.sandbox.root}/work", "task_soul": work.get("soul", ""),
            "scaffold": "", "sandbox_tools": self._sandbox_tools(role),
        } | handler_for(role).extra_variables(self.roles, ticket)
        write = [] if role == cards.RESOLVER else [self.c.p(f"{own}/")]
        context = self._context_refs(ticket, role, tw)
        variables.update({
            "write_scope": ", ".join(f"`{w}`" for w in write) or "(none)",
            "outputs": ", ".join(f"`{o.rstrip('*')}`" + (" (a list)" if o.endswith("*")
                                 else "") for o in outputs) or "(none)",
            "context_refs": "\n".join(f"- {c}" for c in context) or "(none)",
            "findings": self._findings_text(tw),
        })
        return Assignment(variables, write, outputs, context, dict(work), list(tw.findings))

    def _record_assets(self) -> None:
        """Record which asset files (by hash) the change's runs used, once per asset set: a run's
        prompt can then be checked against the current files (Assets.check_prompt)."""
        path = assets_record(self.c.assets.sha)
        if not self.c.latest(path):
            self.c.write_record(path, {"sha": self.c.assets.sha, "files": self.c.assets.manifest()})

    def _payload(self, run: Run, rendered, plan: SandboxPlan, *, tools: bool = True) -> None:
        if run.consult:                       # an A2A answer turn: its prompt is the message
            prompt = rendered.prompt
        else:                                 # the assignment is its task's first message
            run.instructions = rendered.prompt
            prompt = START_PROMPT.format(role=run.role, node=run.ticket, change=self.c.cid,
                                         task=run.id)
        env = self._env(run, tools=tools)
        if found := self.placement.resumable_session(run, plan):
            run.resumed_from, run.resumed_session = found
            env["LEGION_RESUME_SESSION"] = run.resumed_session
        d = write_payload(self.c.run_dir(run.id) / "payload", env, prompt=prompt,
                          soul=rendered.soul, mcp=self._mcp_json())
        write_skills(d / SKILLS_DIR, rendered.skills)
        for rel, text in handler_for(run.role).payload_files(self.roles,
                                                             self._ticket_of(run.ticket),
                                                             run).items():
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)

    async def _stage(self, run: Run, stage: RunStage, message: str) -> None:
        async with self.c.lock:
            if run.status in LIVE_RUN and run.stage != RunStage.RUNNING:
                run.stage = stage
                self.c.event("sandbox", message, run.ticket, run.id)
                self.c.save()

    async def _turn(self, run: Run, proc) -> int:
        """Await the agent's process; its timers stop it (the run timeout while it is live, the
        end-of-turn grace after its accepted report). Each save may change which applies."""
        exited = asyncio.ensure_future(proc.wait())
        try:
            while not exited.done():
                saved = asyncio.ensure_future(self.c.next_save())
                try:
                    await asyncio.wait({exited, saved}, timeout=self.lifecycle.deadline(run),
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    saved.cancel()
                if not exited.done():
                    async with self.c.lock:
                        if self.lifecycle.stop_if_due(run):
                            self.c.save()
            return exited.result()
        finally:
            exited.cancel()

    def _exit_reason(self, run: Run, rc: int) -> tuple[str, FailureSource]:
        """Why a live run's process ended. The agent CLI's own result event means the agent ran
        and ended (an agent error); without one the process or its setup failed (a system
        error). A last turn that ended waiting on the agent's background tasks is not the end:
        the CLI should have resumed when they finished, so its exit is a system error."""
        log = self.lifecycle.log_tail(run.id, 2000)
        result = agentlog.agent_result(log)
        if result is not None and result.pending:
            n = len(result.pending)
            return (f"the agent CLI exited with code {rc} while the agent was waiting on {n} "
                    f"background task{'s' if n != 1 else ''} ({'; '.join(result.pending)[:300]}) "
                    "instead of resuming when they finished – check the agent image's CLI",
                    FailureSource.SYSTEM)
        if result is not None:
            if result.is_error:
                return f"the agent ended with an error ({result.subtype}): {result.message[:500]}", \
                    FailureSource.AGENT
            return (f"the agent finished without an accepted report: {result.message[:300]}",
                    FailureSource.AGENT)
        when = "after connecting" if run.stage == RunStage.RUNNING else "before connecting"
        last = agentlog.last_output(log)
        return (f"agent process exited with code {rc} {when}" + (f": {last}" if last else ""),
                FailureSource.SYSTEM)
