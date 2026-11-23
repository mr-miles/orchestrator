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
  on-complete process – the retrospective – later).
* **Human actions** (decision owners only): a sign-off's decision, a waiting ticket's remedy
  (retry, waive, replan), a replan, pause and resume, abandon.
* **Restart**: :meth:`recover` – every ticket under way goes on at the same attempt (R14).
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from ....git.branches import Branches
from ....git.remote import is_blank_tree
from ... import setup
from ...attest import FILE_RE
from ...environment import EnvironmentSpec, Kind, Library, LibraryError, promote
from ...model import ChangeEnd, ChangeStatus
from ...risk import derive_tier
from ...tickets import Remedy, Replan, Status, Ticket, TicketError, Tickets, Waiting
from ...tickets.executors import (
    AgentExecutor,
    Backoff,
    CanMergeExecutor,
    Executors,
    Merger,
    ProvisionExecutor,
    PublishExecutor,
    SignoffExecutor,
    VerifyExecutor,
)
from ...tickets.plan_doc import parse_plan
from ...tickets.runner import Runner
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
                "controller:preflight": VerifyExecutor(verify, backoff, expect_failures=True),
                "controller:can-merge": CanMergeExecutor(self.branches, backoff),
                "controller:provision": ProvisionExecutor(provision, backoff),
                "controller:publish": PublishExecutor(remote=remote, branches=self.branches,
                                                      base_branch=hub.base_branch,
                                                      message=self._pr_text, backoff=backoff)})
        return self._executors

    def _runner(self) -> Runner:
        if self.runner is None:
            self.runner = Runner(self.c.cid, self.c.tickets, executors=self.executors,
                                 branches=self.branches, base=self.c.change.base_sha or "",
                                 spec=self.spec, hooks=self, save=self._saved,
                                 merger=Merger(self.branches), lock=self.c.lock,
                                 spawner=self.c.spawn)
        return self.runner

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """A new change (the caller holds the lock): its skeleton, then – once the workspace
        is synced – its root starts."""
        self.c.st.tickets.update({x.id: x for x in self.spec.skeleton_at(None)})
        self.c.spawn(self._begin())

    async def _begin(self) -> None:
        """The workspace synced: the change's base, whether the repository is blank, its setup
        pin (not in a blank repository, where what a setup needs does not exist yet)."""
        c = self.c
        try:
            base = await c.git.sync()
            blank = is_blank_tree(await c.git.tree_paths(base))
            pin = None if blank else await setup.pin_at_base(c.git, base)
        except Exception as e:                      # noqa: BLE001 – the change cannot start
            async with c.lock:
                c.change.unsynced = f"workspace {c.change.workspace}: {e}"
                c.event("workspace", f"{c.change.workspace} could not be synced: {e}")
                c.save()
            return
        async with c.lock:
            ch = c.change
            ch.base_sha, ch.blank_repo, ch.setup_pin = base, blank, pin
            ch.unsynced = None
            await c.git.init_integration(c.cid, base)
            c.write_record("control/workspace.json", {
                "workspace": ch.workspace, "source": c.git.source,
                "base_branch": c.git.base_branch, "base": base, "blank_repo": blank})
            c.event("workspace", f"{ch.workspace} synced: the change is based on {base[:12]}"
                    + (" (a blank repository: the plan starts with a scaffold)" if blank else ""))
            c.save()
        runner = self._runner()
        runner.base = base
        await runner.start()

    def load(self) -> None:
        """After the state was loaded: the runner over the loaded tickets."""
        if self.c.st.tickets:
            Tickets.over(self.c.st.tickets)             # checked

    async def recover(self) -> None:
        """After a restart: every ticket under way goes on at the same attempt (R14)."""
        if self.c.st.tickets and self.c.change.base_sha:
            await self._runner().recover()

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
            c.change.completed = True        # its on-complete process: the retrospective, later
            self.lifecycle.release_sandboxes()
            c.save()
        c.spawn(cleanup.end_of_change(c))

    def _saved(self, tickets: Iterable[Ticket]) -> None:
        """The runner's save (the lock is held): kept sandboxes of closed tickets go, then the
        change saves."""
        self.lifecycle.release_sandboxes()
        self.c.save()

    def housekeeping(self) -> None:
        """On every save of the change (the lock is held): kept sandboxes of closed tickets go,
        sandboxes waiting to be stopped are stopped once nothing live uses them."""
        if self.c.st.tickets:
            self.lifecycle.release_sandboxes()
        self.lifecycle.sandboxes.suspend_due()

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
        for them), ``retry`` / ``replan`` (a waiting ticket's remedy), ``waive`` (a rejected,
        waivable criterion of a waiting ticket). Returns what happened."""
        self._owner(approver)
        payload = dict(payload or {})
        c = self.c
        if action in ("approve", "reject"):
            async with c.lock:
                self.signoffs.answer(ticket_id, action == "approve", approver,
                                     str(payload.get("reason") or ""))
                c.save()
            return f"{ticket_id}: {action}d"
        remedy = {"retry": Remedy.RETRY, "replan": Remedy.REPLAN, "waive": Remedy.WAIVE}.get(action)
        if remedy is None:
            raise WorkflowError(f"unknown action {action!r}")
        if remedy == Remedy.WAIVE and not str(payload.get("reason") or "").strip():
            raise WorkflowError("a waiver needs a reason")
        try:
            await self._runner().decide(ticket_id, remedy)
        except TicketError as e:
            raise WorkflowError(str(e)) from None
        async with c.lock:
            c.change.proposals.pop(ticket_id, None)
            c.event("decision", f"{approver}: {action}"
                    + (f" ({payload['reason']})" if payload.get("reason") else ""), ticket_id)
            c.save()
        return f"{ticket_id}: {action}"

    async def change_action(self, action: str, approver: str,
                            payload: Mapping[str, Any] | None = None) -> str:
        """A decision owner's action on the change: ``pause``, ``resume``, ``abandon``,
        ``replan`` (a replan document in ``payload["plan"]``, applied while paused),
        ``promote_proposal`` (an approved environment proposal into the library), ``prune`` (an
        ended change's integration branch and evidence refs, §5.14)."""
        self._owner(approver)
        payload, runner = dict(payload or {}), self._runner()
        if action == "retry" and self.c.change.unsynced:     # the workspace, synced again
            await self._begin()
            return "retried the workspace's sync"
        if action == "prune":
            async with self.c.lock:
                note = await cleanup.prune(self.c, approver)
                self.c.save()
            return note
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
                async with self.c.lock:
                    self.c.change.ended = ChangeEnd(by=approver, node=self.c.cid, action="abandon",
                                                    reason=str(payload.get("reason") or ""))
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
