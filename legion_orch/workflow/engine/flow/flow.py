"""TicketFlow: a change's workflow – its tickets, run by the ticket runner over the services
(plan: ~/plans/tickets-3b.md, E4).

* **Start**: the workspace is synced (the change's base), the root is built from the skeleton
  (``assets/workflow.yaml``) and the runner starts it.
* **The runner** (:mod:`legion_orch.workflow.tickets.runner`) applies the rules under the change's
  lock and carries their effects out; its executors fill their ports with the services
  (:mod:`.adapters`). Every save goes through the change (its state store, journal, bus).
* **Hooks**: the tier from the risk being signed (H-1), a replan from the document a sign-off
  applies (the planner's ``tickets``), a ticket's work checked against its role, an escalation
  (the change's save raises it on the bus: the resolver hears), the end (the change ends; its
  on-complete process follows).
* **On complete** (``hooks.on_complete`` in ``workflow.yaml``): after any ending, once the
  reflections of the sandboxes released at the end are done, the role's agent – the
  retrospective – runs on the ended change, as a ticket outside the tree (``hook`` records); a
  decision owner can ask for it again. The change is *completed* once it is over (then cleaned
  up, §5.14).
* **Human actions** (decision owners only): a sign-off's decision, a waiting ticket's remedy
  (retry, waive, replan), a replan, pause and resume, abandon (with or without the
  retrospective), the retrospective again.
* **Restart**: :meth:`recover` – every ticket under way goes on at the same attempt (R14).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from typing import Any

from ....git.branches import Branches
from ....git.remote import WorkspaceUnsynced, is_blank_tree
from ... import setup
from ...attest import FILE_RE
from ...environment import EnvironmentSpec, Kind, Library, LibraryError, promote
from ...model import ChangeEnd, ChangeStatus
from ...risk import derive_tier
from ...tickets import Remedy, Replan, Status, Ticket, TicketError, Tickets, Waiting
from ...tickets.executors import (
    AgentEnd,
    AgentExecutor,
    Backoff,
    CanMergeExecutor,
    Executors,
    Merger,
    ProvisionExecutor,
    PublishExecutor,
    SignoffExecutor,
    SyncExecutor,
    VerifyExecutor,
    Work,
)
from ...tickets.plan_doc import parse_plan
from ...tickets.runner import Runner
from ...tickets.spec import CONTROLLER_OUTPUTS
from ..base.common import WorkflowError
from ..base.core import ChangeCore
from ..services import cleanup
from ..services.agents import AgentRunner
from ..services.publish import Publisher
from ..services.runs import RunLifecycle
from .adapters import Agents, Provision, Remote, Signoffs, Verify

CLOSED = {Status.PASSED, Status.FAILED, Status.CANCELLED}


class TicketFlow:
    def __init__(self, c: ChangeCore, *, agents: Agents, signoffs: Signoffs, verify: Verify,
                 provision: Provision, publisher: Publisher, agent_runner: AgentRunner,
                 lifecycle: RunLifecycle, backoff: Backoff = Backoff()):
        self.c, self.signoffs, self.agent_runner, self.lifecycle = c, signoffs, agent_runner, lifecycle
        if c.assets.workflow is None:
            raise WorkflowError("the assets have no workflow.yaml")
        self.spec = c.assets.workflow
        self._ports = (agents, verify, provision, publisher, backoff)
        self._executors: Executors | None = None
        self._branches: Branches | None = None
        self.runner: Runner | None = None
        self._merged_branches: set[str] = set()     # branches of passed tickets, deleted
        self._agents = agents
        self.skip_on_complete = False               # abandoned without the retrospective
        self.awaiting_reflections = False           # the on-complete process waits for them

    @property
    def branches(self) -> Branches:
        """The change's hub's ticket branches (the change knows its workspace once it exists)."""
        if self._branches is None:
            self._branches = Branches(self.c.git)
        return self._branches

    @property
    def executors(self) -> Executors:
        """Each ticket type's executor over its port (built once the change exists)."""
        if self._executors is None:
            agents, verify, provision, publisher, backoff = self._ports
            c, hub = self.c, self.c.git
            remote = Remote(c, publisher) if hub.source and c.svc.github.repo_of(hub.source) \
                else None
            self._executors = Executors({
                "agent": AgentExecutor(agents, backoff),
                "human:signoff": SignoffExecutor(self.signoffs, backoff),
                "controller:verify": VerifyExecutor(verify, backoff),
                "controller:sync": SyncExecutor(self, backoff),
                "controller:preflight": VerifyExecutor(verify, backoff, expect_failures=True),
                "controller:baseline": VerifyExecutor(verify, backoff, records="baseline"),
                "controller:can-merge": CanMergeExecutor(self.branches, backoff),
                "controller:provision": ProvisionExecutor(provision, backoff),
                "controller:publish": PublishExecutor(remote=remote, branches=self.branches,
                                                      base_branch=hub.base_branch,
                                                      message=self._pr_text, backoff=backoff)})
        return self._executors

    def _outputs(self) -> dict[str, list[str]]:
        """What each ticket type produces: an agent's its role's outputs, a controller's the
        workflow's (the runner re-signs what a replan revises)."""
        return {f"agent:{r}": o for r, o in self.c.assets.role_outputs().items()} \
            | CONTROLLER_OUTPUTS

    def _runner(self) -> Runner:
        if self.runner is None:
            self.runner = Runner(self.c.cid, self.c.tickets, executors=self.executors,
                                 branches=self.branches, base=self.c.change.base_sha or "",
                                 spec=self.spec, hooks=self, save=self._saved,
                                 merger=Merger(self.branches), lock=self.c.lock,
                                 spawner=self.c.spawn, outputs=self._outputs())
        return self.runner

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """A new change (the caller holds the lock): its skeleton, and its root starts – its
        first ticket syncs the workspace (:meth:`sync`)."""
        self.c.st.tickets.update({x.id: x for x in self.spec.skeleton_at(None)})
        self.c.spawn(self._runner().start())

    async def sync(self, work: Work) -> str | None:
        """``controller:sync`` (its port): the workspace synced, the change's base – whether
        the repository is blank, its setup pin (not in a blank repository, where what a setup
        needs does not exist yet) – and the runner told it. A change that has its base keeps
        it. None when done; why the hub cannot follow its source, else (an outage raises)."""
        c = self.c
        if c.change.base_sha:
            self._runner().base = c.change.base_sha
            return None
        try:
            base = await c.git.sync()
        except WorkspaceUnsynced as e:
            return f"workspace {c.change.workspace}: {e}"
        blank = is_blank_tree(await c.git.tree_paths(base))
        pin = None if blank else await setup.pin_at_base(c.git, base)
        async with c.lock:
            ch = c.change
            ch.base_sha, ch.blank_repo, ch.setup_pin = base, blank, pin
            await c.git.init_integration(c.cid, base)
            c.write_record("control/workspace.json", {
                "workspace": ch.workspace, "source": c.git.source,
                "base_branch": c.git.base_branch, "base": base, "blank_repo": blank})
            c.event("workspace", f"{ch.workspace} synced: the change is based on {base[:12]}"
                    + (" (a blank repository: the plan's scaffold sets the project up)"
                       if blank else ""), work.ticket.id)
            self._runner().base = base
        return None

    def load(self) -> None:
        """After the state was loaded: the runner over the loaded tickets."""
        if self.c.st.tickets:
            Tickets.over(self.c.st.tickets)             # checked

    async def recover(self) -> None:
        """After a restart: every ticket under way goes on at the same attempt (R14)."""
        if self.c.st.tickets:
            await self._runner().recover()
        for t in list(self.c.st.hooks.values()):       # an on-complete process under way
            if t.status == Status.WORKING:
                self.c.spawn(self._hook_round(t))

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.stop()

    # ------------------------------------------------------------------ hooks (the runner's)
    def tier(self, pointers: Mapping[str, Any]) -> str | None:
        """The approved tier: from the risk assessment being (or already) signed."""
        ref = pointers.get("risk")
        m = FILE_RE.match(ref) if isinstance(ref, str) else None
        if m is None:
            return self.c.change.tier
        try:
            risk = derive_tier(json.loads(self.c.docs.read(m["path"], int(m["version"])).content))
        except (ValueError, KeyError, TypeError):
            return self.c.change.tier
        self.c.change.risk, self.c.change.tier = risk, risk.tier
        return risk.tier

    async def replan(self, signoff: Ticket, ref: Any) -> Replan:
        """The replan a sign-off applies: the document it signed."""
        m = FILE_RE.match(ref) if isinstance(ref, str) else None
        if m is None:
            raise ValueError(f"{signoff.id} signed no plan document ({ref!r})")
        try:
            data = json.loads(self.c.docs.read(m["path"], int(m["version"])).content)
        except ValueError as e:
            raise ValueError(f"the plan is not JSON: {e}") from None
        plan, v = parse_plan(data)
        if v:
            raise ValueError("; ".join(v))
        return plan

    def problems(self, ticket: Ticket) -> list[str]:
        return self.c.assets.work_problems(ticket)

    async def escalated(self, ticket: Ticket, reason: Waiting) -> None:
        async with self.c.lock:
            self.c.event("escalation", f"waiting on a human: {reason}", ticket.id)
            self.c.save()

    async def ended(self, status: Status) -> None:
        c = self.c
        async with c.lock:
            c.change.status = ChangeStatus.READY if status == Status.PASSED \
                else ChangeStatus.ABANDONED
            self.agent_runner.close_change()
            c.event("change", f"the change ended: {status}")
            self.lifecycle.release_sandboxes()
            role = self.spec.hooks.get("on_complete")
            auto = role and c.cfg.retrospective.auto and not self.skip_on_complete
            c.change.completed = not auto
            c.save()
            c.trace.end("completed" if status == Status.PASSED else "abandoned")
        c.spawn(self.on_complete() if auto else cleanup.end_of_change(c))

    # ------------------------------------------------------------------ on complete
    async def on_complete(self, focus: str = "") -> None:
        """The ended change's on-complete process: its role's agent (the retrospective), once
        the reflections of the sandboxes released at the end are done – a ticket outside the
        tree, another attempt each time. The change is completed (and cleaned up) once it is
        over, however it ended."""
        c, role = self.c, self.spec.hooks.get("on_complete")
        if not role:
            return
        async with c.lock:
            t = c.st.hooks.get(role)
            if t is not None and t.status == Status.WORKING:
                return
            if t is None:
                t = c.st.hooks[role] = Ticket(id=role, type=f"agent:{role}",
                                              goal="the ended change's retrospective")
            t.status, t.attempt = Status.WORKING, t.attempt + 1
            t.findings = [f"Focus on: {focus}"] if focus else []
            if c.change.ended:
                t.findings.append(f"The change was {c.change.ended.action}ed: why is "
                                  "`change.json` `ended`.")
            c.event(role, f"the change has ended: {role} (attempt {t.attempt}) queued", role)
            c.save()
        await self._hook_round(t)

    async def _hook_round(self, t: Ticket) -> None:
        """A round of a side process's ticket (after a restart: the same attempt again)."""
        c = self.c
        while self.lifecycle.sandboxes.reflector.pending:   # the reflections are its material
            self.awaiting_reflections = True
            try:
                await c.next_save()
            finally:
                self.awaiting_reflections = False
        role = t.type.removeprefix("agent:")
        end = await self._agents.run(role, Work(t, t.attempt, tuple(t.findings), change=c.cid,
                                                base=c.change.base_sha))
        await self._concluded(t, end)

    async def _concluded(self, t: Ticket, end: AgentEnd) -> None:
        c = self.c
        async with c.lock:
            t.status = Status.PASSED if end.accepted else Status.FAILED
            why = end.detail or "it did not report"
            c.event(t.id, f"{t.id} {'done' if end.accepted else 'failed: ' + why}", t.id)
            c.change.completed = True
            c.save()
        await cleanup.end_of_change(c)

    def _saved(self, tickets: Iterable[Ticket]) -> None:
        """The runner's save (the lock is held): kept sandboxes of closed tickets go, then the
        change saves."""
        self.lifecycle.release_sandboxes()
        self.c.save()

    def housekeeping(self) -> None:
        """On every save of the change (the lock is held): kept sandboxes of closed tickets go,
        sandboxes waiting to be stopped are stopped once nothing live uses them, and work waiting
        for a slot is looked at again (an environment it waited for may be built now)."""
        if self.c.st.tickets:
            self.lifecycle.release_sandboxes()
            self._delete_merged_branches()
        self.lifecycle.sandboxes.suspend_due()
        self.c.svc.dispatcher.recheck()            # work waiting for a build may start now

    def _delete_merged_branches(self) -> None:
        """A passed ticket's branch is merged into its parent's (§5.14): it goes; its commits
        stay reachable from there, and every commit an agent reported is pinned as evidence."""
        if self.runner is None:
            return
        for tid, t in self.c.st.tickets.items():
            if t.status == Status.PASSED and t.owns_branch and t.parent is not None \
                    and tid not in self._merged_branches:
                self._merged_branches.add(tid)
                self.c.spawn(cleanup.delete_run_branch(self.c, self.runner.branch_name(tid), tid))

    async def _pr_text(self, work) -> str:
        """The merge message of a publication without a remote: the approved description."""
        ref = self.c.tickets.root.pointers.get("pr")
        m = FILE_RE.match(ref) if isinstance(ref, str) else None
        return self.c.docs.read(m["path"], int(m["version"])).content if m \
            else f"legion {self.c.cid}: {self.c.change.goal}"

    # ------------------------------------------------------------------ human actions
    def _owner(self, approver: str) -> None:
        if approver not in self.c.cfg.decision_owners:
            raise WorkflowError(f"{approver} is not a decision owner "
                                f"({', '.join(self.c.cfg.decision_owners)})")

    def _promote(self, approver: str, payload: Mapping[str, Any]) -> str:
        """Copy a proposal of the approved environment spec (the ``environment`` pointer) into
        the library."""
        ref = self.c.tickets.root.pointers.get("environment") if self.c.st.tickets else None
        m = FILE_RE.match(ref) if isinstance(ref, str) else None
        if m is None:
            raise WorkflowError("there is no approved environment spec")
        try:
            kind = Kind(payload.get("kind"))
        except ValueError:
            raise WorkflowError(f"kind must be one of {[k.value for k in Kind]}") from None
        spec = EnvironmentSpec.model_validate(json.loads(
            self.c.docs.read(m["path"], int(m["version"])).content))
        try:
            dest = promote(Library.for_config(self.c.cfg, self.c.assets.schemas), spec, kind,
                           str(payload.get("name") or ""), f"{ref} (promoted by {approver})")
            Library.for_config(self.c.cfg, self.c.assets.schemas)     # still valid as a whole
        except LibraryError as e:
            raise WorkflowError(str(e)) from None
        self.c.event("library", f"{approver} promoted {kind} {payload.get('name')} into {dest}")
        return f"wrote {dest}"

    async def act(self, ticket_id: str, action: str, approver: str,
                  payload: Mapping[str, Any] | None = None) -> str:
        """A decision owner's action on a ticket: ``approve`` / ``reject`` (a sign-off waiting
        for them), and a waiting ticket's remedies: ``retry`` (its ``answer`` – to a blocked
        agent's question, say – is what its next round is told), ``waive:<criterion>`` (a
        rejected, waivable criterion of it, with a ``reason``), ``replan`` (with its ``plan``:
        the ticket fails and the plan applies as soon as nothing is active; without one, the
        workflow waits paused for a change-level replan). Returns what happened."""
        self._owner(approver)
        payload = dict(payload or {})
        c = self.c
        if action in ("approve", "reject"):
            async with c.lock:
                self.signoffs.answer(ticket_id, action == "approve", approver,
                                     str(payload.get("reason") or ""))
                c.save()
            return f"{ticket_id}: {action}d"
        if action.startswith("waive:"):          # on the waiting ticket: its criterion's waiver
            ticket_id, action = action.removeprefix("waive:"), "waive"
        elif action == "waive":
            raise WorkflowError("waive a criterion on the ticket that waits: waive:<criterion>")
        remedy = {"retry": Remedy.RETRY, "replan": Remedy.REPLAN, "waive": Remedy.WAIVE}.get(action)
        if remedy is None:
            raise WorkflowError(f"unknown action {action!r}")
        if remedy == Remedy.REPLAN and payload.get("plan") is not None:
            plan, v = parse_plan(payload["plan"])
            if v:
                raise WorkflowError("; ".join(v))
            try:
                await self._runner().decide_replan(ticket_id, plan)
            except TicketError as e:
                raise WorkflowError(str(e)) from None
            async with c.lock:
                c.change.proposals.pop(ticket_id, None)
                c.event("decision", f"{approver}: replan", ticket_id)
                c.save()
            return f"{ticket_id}: replan"
        if remedy == Remedy.WAIVE and not str(payload.get("reason") or "").strip():
            raise WorkflowError("a waiver needs a reason")
        answer = str(payload.get("answer") or "").strip()
        told = None
        if remedy == Remedy.RETRY and answer:
            async with c.lock:
                if (t := c.tickets.t.get(ticket_id)) is not None and t.status == Status.WAITING:
                    told, t.findings = list(t.findings), [*t.findings,
                                                          f"Answer from {approver}: {answer}"]
        try:
            await self._runner().decide(ticket_id, remedy)
        except TicketError as e:
            if told is not None:
                async with c.lock:
                    c.tickets.t[ticket_id].findings = told
            raise WorkflowError(str(e)) from None
        async with c.lock:
            c.change.proposals.pop(ticket_id, None)
            c.event("decision", f"{approver}: {action}"
                    + (f" ({payload['reason']})" if payload.get("reason") else ""), ticket_id)
            c.save()
        return f"{ticket_id}: {action}"

    def _ended_by(self, approver: str, reason: str | None) -> None:
        """Who abandoned the change, where it was (the tickets waiting on a human, else the
        root) and why: recorded (``control/ended.json``) – the retrospective reads it."""
        c = self.c
        at = ",".join(t.id for t in c.st.tickets.values() if t.status == Status.WAITING) or c.cid
        end = ChangeEnd(by=approver, node=at, action="abandon", reason=reason)
        end.attestation = c.write_record("control/ended.json",
                                         end.model_dump(mode="json")).attestation
        c.change.ended = end
        c.event("change", f"{approver} abandoned the change at {at}: "
                + (reason or "(no reason given)"))

    async def _on_complete_again(self, approver: str, focus: str = "") -> str:
        """A decision owner asks for the on-complete process (again) on an ended change –
        ``focus``: what to look at, its first message says."""
        c, role = self.c, self.spec.hooks.get("on_complete")
        async with c.lock:
            if not role:
                raise WorkflowError("the workflow has no on-complete process")
            if c.change.status == ChangeStatus.ACTIVE:
                raise WorkflowError(f"a {role} needs an ended change (it is still active)")
            if (t := c.st.hooks.get(role)) is not None and t.status == Status.WORKING:
                raise WorkflowError(f"a {role} is already under way")
            c.event("decision", f"{approver} asked for a {role}", role)
        c.spawn(self.on_complete(focus))
        await asyncio.sleep(0)
        return f"{role} queued"

    async def change_action(self, action: str, approver: str,
                            payload: Mapping[str, Any] | None = None) -> str:
        """A decision owner's action on the change: ``pause``, ``resume``, ``abandon``,
        ``replan`` (a replan document in ``payload["plan"]``, applied while paused),
        ``promote_proposal`` (an approved environment proposal into the library), ``prune`` (an
        ended change's integration branch and evidence refs, §5.14), ``retrospective`` (the
        on-complete process again; ``abandon`` with ``retrospective: false`` skips it)."""
        self._owner(approver)
        payload, runner = dict(payload or {}), self._runner()
        if action == "prune":
            async with self.c.lock:
                note = await cleanup.prune(self.c, approver)
                self.c.save()
            return note
        if action == "retrospective":
            return await self._on_complete_again(approver, str(payload.get("focus") or ""))
        if action == "promote_proposal":
            async with self.c.lock:
                note = self._promote(approver, payload)
                self.c.save()
            return note
        try:
            if action == "pause":
                await runner.pause()
            elif action == "resume":
                await runner.resume()
            elif action == "abandon":
                self.skip_on_complete = payload.get("retrospective", True) is False
                async with self.c.lock:
                    self._ended_by(approver, str(payload.get("reason") or "").strip() or None)
                await runner.abandon()
            elif action == "replan":
                plan, v = parse_plan(payload.get("plan"))
                if v:
                    raise WorkflowError("; ".join(v))
                await runner.replan(plan)
            else:
                raise WorkflowError(f"unknown action {action!r}")
        except TicketError as e:
            raise WorkflowError(str(e)) from None
        async with self.c.lock:
            self.c.event("decision", f"{approver}: {action}")
            self.c.save()
        return action
