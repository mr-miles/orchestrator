"""Agent runs: the factory of an agent's :class:`.Work` – the run record (guards G-D2, G-D4),
its prompt and payload, its sandbox session and commands (the working copy's preparation, then
the agent's turn, :class:`AgentTurn`) – and what the run means once the work is done: the role
handler's result after an accepted report, or the failure's escalation. Whether the step may
start an agent at all (G-B1, the environment) is the phase's question (:meth:`preconditions`)."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ....bus import TurnEnded
from ....config import agent_command
from ....sandbox import (
    AGENT_PREFIX,
    agentlog,
    payload_scripts,
)
from ....store import DocStoreError
from ....util import now_iso
from ... import attest, cards, governance
from ...assets import AssetError, GitMode
from ...model import (
    ACCEPTED,
    H_ENV,
    H_REQUIREMENTS,
    LIVE_RUN,
    PLAN,
    R_ACCEPTED,
    R_FAILED,
    R_REPORTED,
    REWORK,
    CommandResult,
    FailureSource,
    Node,
    NodeKind,
    Resume,
    Run,
    RunStage,
    TaskReply,
)
from ...results import Escalate, PhaseResult
from ...roles import handler_for
from ...view import RoleContext
from ..base.auth import open_task
from ..base.common import ENV_SPEC, PLAN_DOCS, SKILLS_DIR, WorkflowError, _slug, write_skills
from ..base.core import ChangeCore
from ..base.work import AGENT, Command, Outcome, Session, Work
from ..sandboxes.plan import Checkout, SandboxPlan
from ..sandboxes.sandboxes import AgentSession, Sandboxes, sandbox_env
from .agents.placement import Placement
from .runs import RunLifecycle

if TYPE_CHECKING:
    from .agents.tasks import RunTasks
    from .reports import Reports
    from .verification import Verifications


@dataclass
class Assignment:
    """What a run is told: template variables, document scopes and context attestations."""
    variables: dict[str, Any]
    write_scope: list[str]
    required_docs: list[str]
    context: list[str]
    task: dict[str, Any]

    def for_agent(self, node: Node) -> dict[str, Any]:
        """The assignment returned by get_assignment() (the rendered list of context refs is
        replaced by the list itself)."""
        return {k: v for k, v in self.variables.items() if k != "context_refs"} | {
            "task": self.task, "context": self.context,
            "feedback_items": [f.model_dump(mode="json") for f in node.feedback]}


def _file_path(ref: str) -> str | None:
    """The document path of a ``file:<path>@<version>`` attestation (None for git refs)."""
    return ref[5:].rsplit("@", 1)[0] if ref.startswith("file:") and "@" in ref else None


def assets_record(sha: str) -> str:
    return f"control/assets/{sha.removeprefix('sha256:')[:16]}.json"


@dataclass
class Prepared:
    """What an agent's work prepared in its slot: its run, or the guard that refused it."""
    run: Run | None = None
    refused: Escalate | None = None


@dataclass
class Ended:
    """How an agent run ended: its run (None: it never started) and the phase's material – the
    role handler's result after an accepted report (None: nothing to decide yet), or the
    escalation of a failure or a refusal."""
    run: Run | None
    result: PhaseResult | None


START_PROMPT = """You are the `{role}` agent of `{node}` in change {change}.

Your work is a task in your inbox: call `inbox()` (legion MCP) first – task `{task}`, from the
orchestrator, holds your assignment; messages from other agents of the change arrive there too.
Load the `legion-harness-contract` skill before your first action. File your results with
`report(...)`, then end the task with `a2a_reply(task_id="{task}", text=..., state=...)`:
`completed` after an accepted DONE report, `input-required` after an accepted BLOCKED one.
"""

CONTINUE_PROMPT = """The orchestrator, on your task `{task}`:

{message}

Your inbox (`inbox()`) has the task and any messages for you.
"""

RESOLVER_PROMPT = """# A message for the resolver

From `{sender}`:

<message from="{sender}">
{text}
</message>

Answer it in your final message (an escalation: end with your proposal, as your instructions
say). Investigate with your tools and ask other agents over A2A; change nothing.

Your instructions:

{prompt}
"""

CONSULT_PROMPT = """# A message before your work starts

You are the `{role}` agent of `{node}` in change {change}. Your work on this step has not
started yet, but `{sender}` – another agent of this change – sent you this message over A2A:

<message from="{sender}">
{text}
</message>

Answer it. Read what you need – your assignment (get_assignment), the documents, the repository
checkout – but change nothing: no commits, no document writes, no report. Your answer is your
final message, sent back to `{sender}` verbatim. If you cannot answer yet, say so and why.

When the step gets to your work, it continues this session: what you learn now stays with you.
Your instructions for that work, as they stand now (do not start on them in this turn):

{prompt}
"""


@dataclass
class AgentTurn(Command):
    """The agent's turns: start the agent and await its process (its timers stop it: the run
    timeout while its task is open, the end-of-turn grace after it ended it). A turn that ends
    with the run's task still open – no accepted report, or no reply ending the task – is asked
    again (services/inbox.py): the agent's session is resumed for another turn of the same
    run, with the orchestrator's message. Then the run's record is completed (exit code, a live
    run failed with why, the log archived)."""
    runs: AgentRuns | None = None
    of: Run | None = None                   # the run whose agent this is

    async def run(self, session: Session) -> CommandResult:
        runs, run = self.runs, self.of
        c = runs.c
        proc = await runs.sandboxes.start_agent(run.sandbox, c.run_dir(run.id) / "agent.log")
        async with c.lock:
            c.procs[run.id] = proc
            c.event("sandbox", f"agent process started in sandbox {run.sandbox}",
                    run.node_id, run.id)
            c.save()
        rc = await runs._turn(run, proc)
        turn = 1
        while True:
            async with c.lock:
                again = runs.ask_again_after(run, rc)
                c.save()
            if again is None:
                break
            turn += 1
            proc = await runs.continue_turn(run, again, turn)
            async with c.lock:
                c.procs[run.id] = proc
                c.event("agent", f"turn {turn}: the agent's session resumed with the "
                                 "orchestrator's message", run.node_id, run.id)
                c.save()
            rc = await runs._turn(run, proc)
        async with c.lock:
            c.procs.pop(run.id, None)
            run.exit_code = rc
            if run.consult:                 # an A2A turn: its answer is its final message
                run.status, run.ended_at = (R_ACCEPTED if rc == 0 else R_FAILED), now_iso()
                c.event("a2a", f"consult turn exited ({rc})", run.node_id, run.id)
            elif run.status in LIVE_RUN:
                reason, source = runs._exit_reason(run, rc)
                runs.lifecycle.fail_run(run, reason, source, exit_code=rc)
            else:
                c.event("agent", f"agent process exited ({rc})", run.node_id, run.id)
            run.stage = RunStage.EXITED
            runs.lifecycle.archive_log(run)
            if not run.consult:              # what it left in its inbox gets turns of its own
                c.emit(TurnEnded(c.cid, run.node_id, run.role))
            c.save()
        return self.result(rc, "")


class AgentRuns:
    """Makes an agent's work for a node and role, and reads what it did."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, sandboxes: Sandboxes,
                 roles: RoleContext):
        self.c, self.lifecycle, self.sandboxes, self.roles = core, lifecycle, sandboxes, roles
        self.reports: Reports = None  # type: ignore[assignment]   # set once it exists
        self.verifications: Verifications | None = None      # set once it exists (G-V0)
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
        if not (run.status in LIVE_RUN or open_task(run)):
            return None
        # only an agent that ended its turn is asked again: a CLI that crashed, failed or exited
        # waiting on its background tasks fails the run as before (_exit_reason)
        result = agentlog.agent_result(self.lifecycle.log_tail(run.id, 2000))
        if result is None or result.is_error or result.pending:
            return None
        msg = tasks.ask_again(run)
        if msg is None and open_task(run):
            run.reply = TaskReply(state=tasks.wanted(run),
                                  text="(no reply: the accepted report's status is taken)")
            self.c.event("agent", "the task was not ended: its accepted report stands",
                         run.node_id, run.id)
        return msg

    async def continue_turn(self, run: Run, message: str, turn: int):
        """Resume ``run``'s agent session for another turn on ``message`` (its full tools and
        MCP, as its first turn), its log appended to the run's."""
        sid = run.session_id or agentlog.session_id(self.lifecycle.log_tail(run.id, 100_000))
        d = self.c.run_dir(f"{run.id}-turn{turn}") / "payload"
        d.mkdir(parents=True, exist_ok=True)
        (d / "prompt.md").write_text(CONTINUE_PROMPT.format(task=run.id, message=message))
        soul = self.c.assets.soul(run.soul) if run.soul in self.c.assets.souls_ else ""
        (d / "soul.md").write_text(soul)
        (d / "mcp.json").write_text(self._mcp_json())
        env = self._env(run) | {"LEGION_NEEDS_REPO": "0"}
        if sid:
            env["LEGION_RESUME_SESSION"] = sid
        (d / "run.env").write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items()))
        for name, text in payload_scripts().items():
            (d / name).write_text(text)
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

    def _env(self, run: Run) -> dict[str, str]:
        env = {
            "LEGION_RUN_ID": run.id, "LEGION_ROLE": run.role,
            "LEGION_GIT_URL": self.c.git_url, "LEGION_MCP_URL": self.c.mcp_url,
            "LEGION_AGENT_COMMAND": agent_command(self.c.cfg.sandbox),
        }
        # A2A (server/a2a.py): run-agent.sh registers the change's AI Catalog with the a2a CLI;
        # its default agent is the change's orchestrator
        a2a = self.c.cfg.server.a2a_url
        orchestrator = cards.card_url(a2a, cards.ORCHESTRATOR)
        return env | {"LEGION_A2A_CATALOG": cards.catalog_url(a2a),
                      "LEGION_A2A_ORCHESTRATOR": orchestrator,
                      "LEGION_A2A_CARD": cards.card_id(run.role, run.node_id),
                      "A2ACLI_AGENT_CARD": orchestrator}

    # ------------------------------------------------------------------ A2A: an early start
    async def consult(self, node_id: str, role: str, sender: str, text: str) -> str:
        """A message for a step agent that has not started: start it – its sandbox, its soul
        and skills, its assignment as it stands, read-only – for one turn that answers, then
        stop the sandbox. It is the role's agent: later messages resume this session, and the
        role's run, when the step gets to it, continues in this sandbox and session."""
        answerer = self.sandboxes.answerer
        async with answerer.lock(node_id, role):
            if self.lifecycle.agent_state(node_id, role) != "absent" and role != cards.RESOLVER:
                return await answerer.answer_locked(node_id, role, sender, text)   # started
            async with self.c.lock:
                run, plan = self._consult_run(node_id, role, sender, text)
                self.c.event("a2a", f"{role}@{node_id} has not started: starting it "
                                    f"(run {run.id}, sandbox {run.sandbox}) to answer {sender}",
                             node_id, run.id)
                self.c.save()
            fresh = not any(r.sandbox == run.sandbox and r.id != run.id
                            for r in self.c.runs.values())
            work = Work(session=AgentSession(self.sandboxes, run, plan, fresh,
                                             lambda stage, msg: self._stage(run, stage, msg)),
                        commands=[*await self.sandboxes.checkout(run, fresh),
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
                raise WorkflowError(f"{role}@{node_id} could not answer: "
                                    + (out.error or f"no answer (exit {run.exit_code})"))
            async with self.c.lock:
                self.c.event("a2a", f"{role}@{node_id} answered {sender}", node_id, run.id)
                self.c.save()
            return result.message.strip()

    def _consult_run(self, node_id: str, role: str, sender: str, text: str) -> tuple[Run, SandboxPlan]:
        """The record of a consult (under the lock): the role's run as it would be now, in a new
        sandbox (pushing roles get the push provider their run will need), not one of the node's
        runs, with nothing to write and nothing to report."""
        node, ch = self._node_of(node_id), self.c.change
        pushes = self.c.assets.roles[role].git == GitMode.COMMIT
        plan = SandboxPlan(Checkout.BRANCH if pushes else Checkout.BASE)
        at = node.candidate_sha or ch.integration_head or ch.base_sha
        asg = self._assignment(node, role, None, None if pushes else at)
        try:
            rendered = self.c.assets.render_role(role, asg.variables)
        except AssetError as e:
            raise WorkflowError(f"{role}@{node_id}: {e}") from None
        kept = None
        if role == cards.RESOLVER:                     # its one sandbox, for the whole change
            kept = next((r for r in reversed(list(self.c.runs.values()))
                         if r.role == role and r.node_id == node_id and r.sandbox
                         and not r.sandbox_deleted), None)
        run = Run(id=self.c.host.next_run_id(), node_id=node.id, role=role, cycle=node.cycle,
                  soul=node.task.soul if node.task and role == "implementer"
                  else self.c.assets.roles[role].soul, base_sha=node.base_sha or ch.base_sha,
                  judged_sha=None if pushes else at, prompt_pin=rendered.pin,
                  assets_sha=self.c.assets.sha, assignment=asg.for_agent(node),
                  context_refs=asg.context, consult=True,
                  sandbox=kept.sandbox if kept else self.c.host.new_sandbox_name(AGENT_PREFIX),
                  sandbox_ref=kept.sandbox_ref if kept else None)
        self.c.runs[run.id] = run
        template = RESOLVER_PROMPT if role == cards.RESOLVER else CONSULT_PROMPT
        prompt = template.format(role=role, node=node_id, change=ch.id, sender=sender,
                                 text=text, prompt=rendered.prompt)
        self._payload(run, dataclasses.replace(rendered, prompt=prompt), plan)
        return run, plan

    def _node_of(self, node_id: str) -> Node:
        """A step's node, or – the change's resolver, no step's – a stand-in to render its
        assignment against."""
        node = self.c.nodes.get(node_id)
        if node is not None:
            return node
        if node_id == cards.RESOLVER:
            return Node(id=cards.RESOLVER, kind=NodeKind.HUMAN, title="Resolver",
                        agent_roles=[cards.RESOLVER])
        raise WorkflowError(f"unknown step {node_id}")

    # ------------------------------------------------------------------ for the phase
    def preconditions(self, node: Node, role: str) -> Escalate | None:
        """Whether the step may start this agent now (guard G-B1: every H-2 subject approved
        and fresh; the approved environment not stale). The scaffold of a blank repository is
        built before H-2, on the critique's PASS (§5.3b)."""
        h, ch = handler_for(role), self.c.change
        resume = Resume.REWORK if node.status == REWORK else Resume.READY
        if h.needs_fresh_approval and not (node.task and node.task.scaffold):
            if stale := self._approval_fresh():
                return Escalate("approval", "; ".join(stale), resume)
        if sandbox_env(ch, role) and (stale_env := governance.stale_environment(self.c)):
            return Escalate("approval", stale_env, resume)
        if h.judge and node.task and node.candidate_sha and self.verifications is not None:
            # G-V0: judges see verified code only – a passing verification of the candidate
            # under the key as it is now (a setup pin or verifier image changed since: again)
            svc = self.verifications
            got = svc.result(svc.key(node.candidate_sha, node.task.verification))
            if got is None or got.state != "passed":
                return Escalate("unverified", f"G-V0: candidate {node.candidate_sha[:12]} has no "
                                "passing verification under the current setup and verifier "
                                "image: verify it again (retry) before it is judged",
                                Resume.VERIFYING)
        return None

    def work(self, node: Node, role: str, plan: SandboxPlan, judged_sha: str | None, *,
             wanted: Callable[[], bool], started: Callable[[Run], None]) -> tuple[Work, Prepared]:
        """The agent's work. It is prepared in its slot, under the lock: unless the step no
        longer ``wanted`` it, the run record is made (or a guard refuses it) and ``started`` is
        told; then its sandbox session and commands."""
        prepared = Prepared()

        async def prepare(work: Work) -> bool:
            async with self.c.lock:
                if not wanted():
                    return False
                run = self.prepare(node, role, plan, judged_sha)
                if isinstance(run, Escalate):
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
        """Under the lock, however the work ended (also when its step no longer wants the
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

    async def conclude(self, run: Run) -> PhaseResult | None:
        """Under the lock: what the run means – its report's, through its role handler (None:
        nothing to decide yet), or its failure's escalation."""
        if run.status == R_REPORTED:
            return await self.reports.conclude(run)
        return self.lifecycle.escalation(run) if run.failure else None

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
    def _approval_fresh(self) -> list[str]:
        """Guard G-B1: every H-2 subject (plan, each delta spec, oracle) approved and fresh."""
        return governance.stale_approvals(self.c)

    def _plan_docs(self) -> list[str]:
        return PLAN_DOCS + [f"plan/specs/{r}.md" for r in self.c.change.requirements]

    def _context_refs(self, node: Node, role: str) -> list[str]:
        """Common context plus the role handler's own (workflow/roles/). A build task's roles get
        only its own requirements' delta specs; a document the controller gives (at its approved
        version) is not repeated at another version from the task's declared context."""
        h, cid = handler_for(role), self.c.cid
        refs: list[str] = []
        appr1 = self.c.change.approvals.get(H_REQUIREMENTS)
        if appr1 and h.sees_requirements:
            refs += [f"file:{p}@{v}" for p, v in appr1.pins.items()
                     if h.sees_risk or p != self.c.p("intake/risk.json")]
        if h.sees_draft_plan or not self.c.change.approvals.get(governance.approval_key("plan")):
            if (plan := self.c.current(PLAN)) and plan.status == ACCEPTED:
                refs += [a for a in (self.c.latest_att(p) for p in self._plan_docs()) if a]
        else:
            own = ({self.c.p(f"plan/specs/{r}.md") for r in node.task.requirements}
                   if h.task_scoped_specs and node.task else None)
            for key, appr in self.c.change.approvals.items():
                if key == "oracle" or key.startswith("spec:"):
                    refs += [f"file:{p}@{v}" for p, v in appr.pins.items()
                             if own is None or not p.startswith(self.c.p("plan/specs/")) or p in own]
        given = {_file_path(a) for a in refs} - {None}
        refs += [a for a in (attest.normalize(x, cid) for x in h.extra_context(self.roles, node))
                 if _file_path(a) not in given]
        if self.c.change.oracle_sha and h.sees_oracle_commit:
            refs.append(f"git:{self.c.workspace}@{self.c.change.oracle_sha}")
        return list(dict.fromkeys(refs))

    def _feedback_text(self, node: Node) -> str:
        if not any(f.cycle >= node.round_start_cycle for f in node.feedback):
            return "(none)"
        lines, head = [], self.c.change.integration_head
        for f in node.feedback:
            if f.cycle < node.round_start_cycle:     # an earlier round's (a fix round starts fresh)
                continue
            atts = ", ".join(f.attestations)
            at = ""
            if f.head:
                at = (f" (given at integration head {f.head[:12]}, the current head)" if f.head == head
                      else f" (given at integration head {f.head[:12]}; the head is now {head[:12]})")
            lines.append(f"- cycle {f.cycle} [{f.source}] {f.text}" + at
                         + (f" (evidence: {atts})" if atts else ""))
        return "\n".join(lines)

    def prepare(self, node: Node, role: str, plan: SandboxPlan,
                judged_sha: str | None = None) -> Run | Escalate:
        """Create a run record under the lock (guards G-D1, G-D2, G-D4), or return why it cannot
        run (its prompt does not render)."""
        ch = self.c.change
        if plan.base_at_head(bool(node.base_sha)):
            node.base_sha = ch.integration_head
        if plan.at_head:
            judged_sha = ch.integration_head
        run_id = self.c.host.next_run_id()
        # only roles that push get a branch (one per node: a fix round's oracle is its own node,
        # oracle@n, so its branch starts afresh); the others check their commit out detached
        branch = f"legion/{ch.id}/{_slug(node.id)}" if plan.pushes else None
        asg = self._assignment(node, role, branch, judged_sha)
        try:
            rendered = self.c.assets.render_role(role, asg.variables)
        except AssetError as e:
            return Escalate("assets", str(e),
                            Resume.REWORK if node.status == REWORK else Resume.READY)
        run = Run(id=run_id, node_id=node.id, role=role, cycle=node.cycle,
                  soul=node.task.soul if node.task and role == "implementer"
                  else self.c.assets.roles[role].soul,
                  base_sha=node.base_sha or ch.base_sha, branch=branch, judged_sha=judged_sha,
                  prompt_pin=rendered.pin, assets_sha=self.c.assets.sha,
                  template_vars={k: str(v) for k, v in asg.variables.items()},
                  write_scope=asg.write_scope, required_docs=asg.required_docs,
                  context_refs=asg.context, assignment=asg.for_agent(node))
        self._record_assets()
        self.placement.place(run, node, plan, judge=handler_for(role).judge)
        self._record_dispatch(run, node, plan)
        self._payload(run, rendered, plan)
        self.c.event("dispatch", f"{role} run {run_id} (cycle {run.cycle}, soul {run.soul}, "
                     f"pin {rendered.pin[7:19]})", node.id, run_id)
        return run

    def _sandbox_tools(self, role: str) -> str:
        """What the role's sandbox has for the project, stated as a fact: its environment
        profile's tools, or – without a provisioned environment – the default image, to check."""
        ch = self.c.change
        env = ch.environment.roles.get(role) if ch.environment else None
        appr = ch.approvals.get(H_ENV)
        tools: list[str] = []
        if env is not None and appr is not None:
            try:
                spec = self.c.json_doc(ENV_SPEC, appr.pins.get(self.c.p(ENV_SPEC)))
                tools = list(spec["profiles"][env.profile]["tools"])
            except (KeyError, TypeError, ValueError, DocStoreError):
                tools = []
        if not tools:
            return ("the operator's default image: check a tool with `command -v` before relying "
                    "on it – do not assume python3, node or rsync; verification runs in the "
                    "verifier image, which may have less")
        return (f"profile `{env.profile}`: {', '.join(tools)}. Build, test and verify only with "
                "these (the verifier has them too); anything else on PATH, such as python3, is "
                "at most for your own edits, never for a verification command")

    def _assignment(self, node: Node, role: str, branch: str | None,
                    judged_sha: str | None) -> Assignment:
        """The template variables, document scopes and context of a run (guard G-D2 binds them)."""
        ch, task = self.c.change, node.task
        task_json = task.model_dump(mode="json") if task else {}
        variables = {
            "change_id": ch.id, "goal": ch.goal, "tier": ch.tier or "unassessed",
            "repo_name": self.c.workspace,
            "base_sha": node.base_sha or ch.base_sha, "head_sha": ch.integration_head,
            "change_base_sha": ch.base_sha or "",             # the change's whole diff from here
            "oracle_sha": ch.oracle_sha or "",
            "branch": branch or "(none: a detached checkout of the commit you work at)",
            "task_id": task.id if task else node.id,
            "task_title": task.title if task else node.title,
            "task_json": json.dumps(task_json, indent=2),
            "cycle": node.cycle, "role": role, "candidate_sha": judged_sha or "",
            "workdir": f"{self.c.cfg.sandbox.root}/work", "task_soul": task.soul if task else "",
            "fix_round": "", "scaffold": "", "sandbox_tools": self._sandbox_tools(role),
        } | handler_for(role).extra_variables(self.roles, node)
        write, required = self.c.assets.role_paths(role, variables)
        write, required = [self.c.p(w) for w in write], [self.c.p(d) for d in required]
        context = self._context_refs(node, role)
        variables.update({
            "write_scope": ", ".join(f"`{w}`" for w in write),
            "required_docs": ", ".join(f"`{d}`" for d in required) or "(none)",
            "context_refs": "\n".join(f"- {c}" for c in context) or "(none)",
            "feedback": self._feedback_text(node),
        })
        return Assignment(variables, write, required, context, task_json)

    def _record_assets(self) -> None:
        """Record which asset files (by hash) the change's runs used, once per asset set: a run's
        prompt can then be checked against the current files (Assets.check_prompt)."""
        path = assets_record(self.c.assets.sha)
        if not self.c.latest(path):
            self.c.write_record(path, {"sha": self.c.assets.sha, "files": self.c.assets.manifest()})

    def _record_dispatch(self, run: Run, node: Node, plan: SandboxPlan) -> None:
        h, ch = handler_for(run.role), self.c.change
        self.c.runs[run.id] = run
        node.runs.append(run.id)
        if not h.panel:                 # panel members run alongside; the node stays REVIEWING
            node.current_run = run.id
        if plan.keeps and node.sandbox is None:
            node.sandbox = run.sandbox
        # approved_spec_and_oracle compares with these (the scaffold precedes H-2, §5.3b)
        if node.task and plan.pushes and not node.task.scaffold:     # a build run
            if ch.first_build_at is None:
                ch.first_build_at = now_iso()
            rnd = next((x for x in ch.rounds if x.number == node.round), None)
            if rnd and rnd.first_build_at is None:
                rnd.first_build_at = now_iso()

    def _payload(self, run: Run, rendered, plan: SandboxPlan) -> None:
        d = self.c.run_dir(run.id) / "payload"
        d.mkdir(parents=True, exist_ok=True)
        (d / "soul.md").write_text(rendered.soul)
        if run.consult:                       # an A2A answer turn: its prompt is the message
            (d / "prompt.md").write_text(rendered.prompt)
        else:                                 # the assignment is its task's first message
            run.instructions = rendered.prompt
            (d / "prompt.md").write_text(START_PROMPT.format(
                role=run.role, node=run.node_id, change=self.c.cid, task=run.id))
        write_skills(d / SKILLS_DIR, rendered.skills)
        (d / "mcp.json").write_text(self._mcp_json())
        env = self._env(run)
        if found := self.placement.resumable_session(run, plan):
            run.resumed_from, run.resumed_session = found
            env["LEGION_RESUME_SESSION"] = run.resumed_session
        (d / "run.env").write_text("".join(f"{k}={shlex.quote(str(v))}\n" for k, v in env.items()))
        for name, text in payload_scripts().items():
            (d / name).write_text(text)
        for rel, text in handler_for(run.role).payload_files(self.roles,
                                                             self._node_of(run.node_id),
                                                             run).items():
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)

    async def _stage(self, run: Run, stage: RunStage, message: str) -> None:
        async with self.c.lock:
            if run.status in LIVE_RUN and run.stage != RunStage.RUNNING:
                run.stage = stage
                self.c.event("sandbox", message, run.node_id, run.id)
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
