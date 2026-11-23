"""The runner (plan: ~/plans/tickets-design.md §4): one change's tickets, driven. It applies every
event to the rules (:class:`.Tickets`) under the change's lock and carries out the effects:

* ``StartWork`` – the ticket's :class:`.Work` (its branch made along the tree – a ticket's starts
  at its parent's head –, the commit a criterion judges, the branch that merges into), then its
  executor's run as a task; the outcome comes back as the rules' event (:func:`.apply`), unless the
  round is over by then (stopped, abandoned: dropped);
* ``Merge`` – the merger merges the commit into the parent's branch (:mod:`.executors.merging`);
* ``StopWork`` – the task cancelled and the executor told;
* ``Escalate``, ``Ended`` – the hooks (``on_escalation``, ``on_complete``).

A sign-off that ``applies`` something pauses the workflow while it is under way (the rules) and
passes by applying it, in the same step: ``tier`` – the tier's skeleton tickets –, or a replan
document (the planner's tickets, a resolver's proposal), which must keep the tier's policy
(:func:`.check_policy`) – a tree that does not is the sign-off's failure, its violations the
findings (so the planner reworks). If it passes while other tickets are still active (draining
to their conclusion points), its verdict is held until they are not.

After a restart (:meth:`Runner.recover`, R14): every working ticket's executor runs again, at the
same attempt – an executor adopts the run it had if that is still alive (an agent's sandbox and
session), so a restart is never an attempt; a merging ticket merges again (a merge done before
the restart is found merged)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import replace
from typing import Any, Protocol

from ...git.branches import Branches, integration_branch, ticket_branch
from ...git.remote import GitError
from .executors import Done, Executors, Fault, Judged, Merger, Outcome, Work, apply, merge_outcome
from .model import Remedy, Status, Ticket, Waiting
from .outputs import TIER, resolve
from .rules import Effect, Ended, Escalate, Merge, Replan, StartWork, StopWork, TicketError, Tickets
from .spec import WorkflowSpec, check_policy

log = logging.getLogger(__name__)


class Hooks(Protocol):
    """What the runner needs from the rest of the controller."""

    def tier(self, pointers: Mapping[str, Any]) -> str | None: ...   # from the approved risk

    async def replan(self, signoff: Ticket, ref: Any) -> Replan: ...  # the signed replan, read

    def problems(self, ticket: Ticket) -> list[str]: ...        # its work for its role

    async def escalated(self, ticket: Ticket, reason: Waiting) -> None: ...

    async def ended(self, status: Status) -> None: ...


class Runner:
    def __init__(self, change: str, tickets: Tickets, *, executors: Executors, branches: Branches,
                 base: str, spec: WorkflowSpec, hooks: Hooks,
                 save: Callable[[Iterable[Ticket]], None], merger: Merger | None = None,
                 lock: asyncio.Lock | None = None,
                 spawner: Callable[[Coroutine], asyncio.Task] | None = None):
        self.change, self.t, self.executors, self.branches = change, tickets, executors, branches
        self.base, self.spec, self.hooks, self.save = base, spec, hooks, save
        self.merger = merger or Merger(branches)
        self.lock = lock or asyncio.Lock()        # the engine's: the change's own lock
        self.spawner = spawner or asyncio.ensure_future   # the engine's: its tasks are tracked
        self._work: dict[str, asyncio.Task] = {}        # ticket -> its round's task
        self._tasks: set[asyncio.Task] = set()          # every task until it is done
        self._held: tuple[str, Outcome] | None = None   # a sign-off's verdict, until quiet

    # ------------------------------------------------------------------ events
    async def start(self) -> None:
        async with self.lock:
            await self._carry(self.t.start())

    async def decide(self, tid: str, remedy: Remedy) -> None:
        async with self.lock:
            await self._carry(self.t.decide(tid, remedy))

    async def pause(self, reason: str = "operator") -> None:
        """Nothing starts until :meth:`resume`: work under way goes on to its conclusion."""
        async with self.lock:
            await self._carry(self.t.pause(reason))

    async def resume(self, reason: str = "operator") -> None:
        async with self.lock:
            await self._carry(self.t.resume(reason))

    async def replan(self, plan: Replan) -> None:
        """A replan signed off outside a criterion (a human's, on an escalation): applied
        while the workflow is paused and nothing is active – prepared and checked as a
        signed-off one is (what the workflow says of its types, their work, the tier's
        policy)."""
        async with self.lock:
            tier = self._tier()
            if v := self._prepare(plan, tier) or (self._policy(plan, tier) if tier else []):
                raise TicketError("; ".join(v))
            await self._carry(self.t.replan(plan))

    async def abandon(self) -> None:
        async with self.lock:
            await self._carry(self.t.abandon())

    async def recover(self) -> None:
        """After a restart: each ticket under way goes on at the same attempt (R14)."""
        async with self.lock:
            await self._carry(
                [Merge(x.id, x.sha, x.parent) if x.status == Status.MERGING  # type: ignore[arg-type]
                 else StartWork(x.id, x.attempt, tuple(x.findings))
                 for x in self.t.t.values() if x.status in (Status.MERGING, Status.WORKING)])

    async def settle(self) -> None:
        """Until nothing runs (tests, shutdown)."""
        # a finished task leaves the set by a callback, which the loop runs later: wait only on
        # the unfinished ones (waiting on finished ones returns without yielding – a busy loop)
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def stop(self) -> None:
        """The controller stops: work is cancelled, not ended (a restart resumes it)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ effects
    async def _carry(self, effects: list[Effect]) -> None:
        """Save the tickets as the event left them, then carry out ``effects`` (under the lock):
        a restart in the middle finds the state the effects came from."""
        self.save(self.t.t.values())
        for e in effects:
            match e:
                case StartWork():
                    work = await self._work_of(e)
                    self._work[e.ticket] = self._spawn(self._run(e.ticket, work))
                case StopWork(ticket):
                    if task := self._work.pop(ticket, None):
                        task.cancel()
                    if (x := self.t.t[ticket]).has_work:
                        self._spawn(self.executors(x.type).stop(ticket))
                case Merge(ticket, sha, into):
                    self._spawn(self._merge(ticket, sha, into))
                case Escalate(ticket, reason):
                    self._spawn(self.hooks.escalated(self.t.t[ticket], reason))
                case Ended(status):
                    self._spawn(self.hooks.ended(status))
        if self._held and self.t.active() == [self._held[0]]:     # the others have drained
            (tid, outcome), self._held = self._held, None
            await self._carry(await self._outcome(self.t.t[tid], outcome))

    def _spawn(self, coro) -> asyncio.Task:
        task = self.spawner(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_report)
        return task

    async def _run(self, tid: str, work: Work) -> None:
        x = self.t.t[tid]
        try:
            outcome = await self.executors(x.type).run(work)
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # noqa: BLE001 – executors return faults
            log.exception("executor of %s", tid)
            outcome = Fault(f"{type(e).__name__}: {e}")
        async with self.lock:
            if self._work.get(tid) is not asyncio.current_task():
                return                                # the round is over: its outcome is moot
            del self._work[tid]
            x = self.t.t[tid]
            if x.status != Status.WORKING or x.attempt != work.attempt:
                return
            if self._holds(x, outcome):
                self._held = (tid, outcome)
                return
            try:
                await self._carry(await self._outcome(x, outcome))
            except Exception:                         # a bug: the ticket waits on a human
                log.exception("ticket %s: carrying out its outcome", tid)
                if self.t.t[tid].status == Status.WORKING:
                    await self._carry(self.t.system_error(tid))

    def _holds(self, x: Ticket, outcome: Outcome) -> bool:
        """A sign-off that applies a replan passed while others are still active: its verdict
        waits for them (the replan's static backdrop)."""
        return bool(x.role == "criterion" and x.applies and _passed(outcome)
                    and [t for t in self.t.active() if t != x.id])

    async def _outcome(self, x: Ticket, outcome: Outcome) -> list[Effect]:
        """The outcome as the rules' event – a passing sign-off that applies something applies
        it (or fails, with why)."""
        if x.role == "criterion" and x.applies and _passed(outcome):
            signed = outcome.signed if isinstance(outcome, Judged) else None
            try:
                plan, violations = await self._applied(x, signed or {})
                if not violations:
                    return self.t.verdict(x.id, True, apply=plan, signed=signed)
            except (TicketError, ValueError) as e:    # refused: nothing of it was applied
                violations = [f"the plan cannot be applied: {e}"]
            return self.t.verdict(x.id, False, violations)
        return apply(self.t, x.id, outcome)

    def _tier(self, signed: Mapping[str, Any] | None = None) -> str | None:
        """The approved tier – with what is being signed now (H-1 signs the risk it comes from)."""
        return self.hooks.tier({**self.t.root.pointers, **(signed or {})})

    async def _applied(self, x: Ticket, signed: Mapping[str, Any]
                       ) -> tuple[Replan, list[str]]:
        """What a sign-off applies, and what is wrong with the tree it makes (the policy)."""
        tier = self._tier(signed)
        if x.applies == TIER:
            if tier is None:
                raise ValueError("no tier has been assessed")
            return self.spec.tier_additions(tier, self.t.t), []
        if x.applies not in signed:
            raise ValueError(f"it applies {x.applies!r}, which it did not sign")
        plan = await self.hooks.replan(x, signed[x.applies])
        problems = self._prepare(plan, tier)
        return plan, problems or (self._policy(plan, tier, signoff=x.id) if tier else [])

    def _prepare(self, plan: Replan, tier: str | None) -> list[str]:
        """What the workflow says of a replan's new tickets' types (waivable, attempt limits),
        and what is wrong with any work it gives (its role's)."""
        problems = []
        for a in plan.add:
            for y in [a.ticket, *a.tree]:
                self.spec.prepare(y, tier)
                problems += self.hooks.problems(y)
        for tid, fields in plan.update.items():
            if "work" in fields and tid in self.t.t:
                problems += self.hooks.problems(self.t.t[tid].model_copy(update=fields))
        return problems

    def _policy(self, plan: Replan, tier: str, signoff: str | None = None) -> list[str]:
        """The tier policy's violations in the tree the replan would make (a trial on a copy)."""
        trial = Tickets(x.model_copy(deep=True) for x in self.t.t.values())
        if signoff is not None:
            trial.verdict(signoff, True, apply=plan)
        else:
            trial.replan(plan)
        return check_policy(self.spec, tier, trial.t)

    async def _merge(self, tid: str, sha: str, into: str) -> None:
        x = self.t.t[tid]
        branch = await self.branch(into)
        done = await self.merger.merge(sha, branch, f"legion {self.change}: {x.goal or x.id}")
        async with self.lock:
            if self.t.t[tid].status == Status.MERGING:
                await self._carry(merge_outcome(self.t, tid, branch, done))

    # ------------------------------------------------------------------ work and branches
    def branch_name(self, tid: str) -> str:
        """A branch owner's branch: the root's is the change's integration branch."""
        x = self.t.t[tid]
        return integration_branch(self.change) if x.parent is None else ticket_branch(self.change, tid)

    def owner(self, tid: str) -> str:
        """The ticket whose branch ``tid`` works on: itself if it owns one, else the nearest
        ancestor that does."""
        return tid if self.t.t[tid].owns_branch else self.t.merges_into(tid)

    async def branch(self, tid: str) -> str:
        """The branch ``tid`` works on (:meth:`owner`'s), made if there is none yet: from the
        head of the branch it merges into (the root's, from the change's base)."""
        tid = self.owner(tid)
        name = self.branch_name(tid)
        if await self.branches.head(name) is None:
            at = self.base if self.t.t[tid].parent is None else await self.branches.head(
                await self.branch(self.t.merges_into(tid)))
            if at is None:
                raise GitError(f"no head to start {name} from")
            await self.branches.create(name, at)
        return name

    def _subjects(self, x: Ticket, judged: str | None) -> dict[str, Any] | None:
        """A sign-off's subjects, resolved (None: they cannot be – logged, its executor faults)."""
        if not x.signs:
            return None
        found, errs = resolve(self.t.t, x.id, judged)
        if errs:
            log.warning("ticket %s: %s", x.id, "; ".join(errs))
            return None
        return found

    async def _work_of(self, e: StartWork) -> Work:
        x = self.t.t[e.ticket]
        work = Work(ticket=x.model_copy(deep=True), attempt=e.attempt, findings=e.findings,
                    change=self.change, labels=frozenset(self.t.labels(x.id)))
        work = replace(work, inputs={i: self.t.root.pointers.get(i) for i in x.inputs})
        if x.parent is None or not self.base:       # nothing has a branch before the change
            return work                             # has a base (its sync is the first work)
        try:
            if x.role == "criterion":                 # it judges its owner's commit
                owner = self.t.t[x.parent]
                judged = owner.sha or await self.branches.head(await self.branch(owner.id))
                into = await self.branch(self.t.merges_into(owner.id)) if owner.parent else None
                return replace(work, judged=judged, into=into, subjects=self._subjects(x, judged))
            base = await self.branches.head(await self.branch(self.t.merges_into(x.id)))
            if x.owns_branch:
                return replace(work, branch=await self.branch(x.id), base=base)
            return replace(work, base=base)
        except GitError as e:                         # the executor's faults say it, later
            log.warning("ticket %s: %s", x.id, e)
            return work


def _report(task: asyncio.Task) -> None:
    """A task of the runner's that failed (a bug: it is logged, never lost)."""
    if not task.cancelled() and (e := task.exception()) is not None:
        log.error("ticket runner task failed", exc_info=e)


def _passed(outcome: Outcome) -> bool:
    return isinstance(outcome, Done) or isinstance(outcome, Judged) and outcome.passed
