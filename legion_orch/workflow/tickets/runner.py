"""The runner (plan: ~/plans/tickets-design.md §4): one change's tickets, driven. It applies every
event to the rules (:class:`.Tickets`) under the change's lock and carries out the effects:

* ``StartWork`` – the ticket's :class:`.Work` (its branch made along the tree – a ticket's starts
  at its parent's head –, the commit a criterion judges, the branch that merges into), then its
  executor's run as a task; the outcome comes back as the rules' event (:func:`.apply`), unless the
  round is over by then (stopped, abandoned: dropped);
* ``Merge`` – the merger merges the commit into the parent's branch (:mod:`.executors.merging`);
* ``StopWork`` – the task cancelled and the executor told;
* ``Escalate``, ``Ended`` – the hooks (``on_escalation``, ``on_complete``).

A sign-off that ``applies`` something passes by applying it, in the same step (nothing starts
first): ``tier`` – the tier's skeleton tickets –, or a replan document (the planner's tickets, a
resolver's proposal), which must keep the tier's policy (:func:`.check_policy`) – a tree that
does not is the sign-off's failure, its violations the findings (so the planner reworks).

After a restart (:meth:`Runner.resume`, R14): a working agent ticket waits on a human – its run is
gone, which is never an attempt –; a working controller ticket runs again; a merging ticket merges
again (a merge done before the restart is found merged)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Protocol

from ...git.branches import Branches, ticket_branch
from ...git.remote import GitError
from .executors import Done, Executors, Fault, Judged, Merger, Outcome, Work, apply, merge_outcome
from .model import GROUPING, Remedy, Status, Ticket, Waiting
from .rules import Effect, Ended, Escalate, Merge, Replan, StartWork, StopWork, TicketError, Tickets
from .spec import TIER, WorkflowSpec, check_policy

log = logging.getLogger(__name__)


class Hooks(Protocol):
    """What the runner needs from the rest of the controller."""

    def tier(self) -> str | None: ...

    async def replan(self, signoff: Ticket) -> Replan: ...       # the document it signs, read

    async def escalated(self, ticket: Ticket, reason: Waiting) -> None: ...

    async def ended(self, status: Status) -> None: ...


class Runner:
    def __init__(self, change: str, tickets: Tickets, *, executors: Executors, branches: Branches,
                 base: str, spec: WorkflowSpec, hooks: Hooks,
                 save: Callable[[Iterable[Ticket]], None], merger: Merger | None = None):
        self.change, self.t, self.executors, self.branches = change, tickets, executors, branches
        self.base, self.spec, self.hooks, self.save = base, spec, hooks, save
        self.merger = merger or Merger(branches)
        self.lock = asyncio.Lock()
        self._work: dict[str, asyncio.Task] = {}        # ticket -> its round's task
        self._tasks: set[asyncio.Task] = set()          # every task until it is done

    # ------------------------------------------------------------------ events
    async def start(self) -> None:
        async with self.lock:
            await self._carry(self.t.start())

    async def decide(self, tid: str, remedy: Remedy) -> None:
        async with self.lock:
            await self._carry(self.t.decide(tid, remedy))

    async def replan(self, plan: Replan) -> None:
        """A replan signed off outside a criterion (a human's, on an escalation)."""
        async with self.lock:
            if (tier := self.hooks.tier()) and (v := self._policy(plan, tier)):
                raise TicketError("; ".join(v))
            await self._carry(self.t.replan(plan))

    async def abandon(self) -> None:
        async with self.lock:
            await self._carry(self.t.abandon())

    async def resume(self) -> None:
        """After a restart: each ticket that was under way goes on as R14 says."""
        async with self.lock:
            for x in list(self.t.t.values()):
                if x.status == Status.MERGING:
                    await self._carry([Merge(x.id, x.sha, x.parent)])       # type: ignore[arg-type]
                elif x.status == Status.WORKING and x.type.startswith("agent:"):
                    await self._carry(self.t.system_error(x.id))          # its run is gone
                elif x.status == Status.WORKING:
                    await self._carry([StartWork(x.id, x.attempt, tuple(x.findings))])

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

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
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
            try:
                await self._carry(await self._outcome(x, outcome))
            except Exception:                         # a bug: the ticket waits on a human
                log.exception("ticket %s: carrying out its outcome", tid)
                if self.t.t[tid].status == Status.WORKING:
                    await self._carry(self.t.system_error(tid))

    async def _outcome(self, x: Ticket, outcome: Outcome) -> list[Effect]:
        """The outcome as the rules' event – a passing sign-off that applies something applies
        it (or fails, with why)."""
        passed = isinstance(outcome, Done) or isinstance(outcome, Judged) and outcome.passed
        if x.role == "criterion" and x.applies and passed:
            try:
                plan, violations = await self._applied(x)
                if not violations:
                    return self.t.verdict(x.id, True, apply=plan)
            except (TicketError, ValueError) as e:    # refused: nothing of it was applied
                violations = [f"the plan cannot be applied: {e}"]
            return self.t.verdict(x.id, False, violations)
        return apply(self.t, x.id, outcome)

    async def _applied(self, x: Ticket) -> tuple[Replan, list[str]]:
        """What a sign-off applies, and what is wrong with the tree it makes (the policy)."""
        tier = self.hooks.tier()
        if x.applies == TIER:
            if tier is None:
                raise ValueError("no tier has been assessed")
            return self.spec.tier_additions(tier, self.t.t), []
        plan = await self.hooks.replan(x)
        for a in plan.add:                            # what the workflow says of their types
            for y in [a.ticket, *a.tree]:
                self.spec.prepare(y, tier)
        return plan, (self._policy(plan, tier, signoff=x.id) if tier else [])

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
        x = self.t.t[tid]
        return f"legion/{self.change}/{x.branch}" if x.branch else ticket_branch(self.change, tid)

    async def branch(self, tid: str) -> str:
        """The ticket's branch, made if it has none yet: from its parent's head (the root's,
        from the change's base)."""
        x, name = self.t.t[tid], self.branch_name(tid)
        if await self.branches.head(name) is None:
            at = self.base if x.parent is None else await self.branches.head(
                await self.branch(x.parent))
            if at is None:
                raise GitError(f"no head to start {name} from")
            await self.branches.create(name, at)
        return name

    async def _work_of(self, e: StartWork) -> Work:
        x = self.t.t[e.ticket]
        work = Work(ticket=x.model_copy(deep=True), attempt=e.attempt, findings=e.findings,
                    change=self.change, labels=frozenset(self.t.labels(x.id)))
        if x.parent is None:
            return work
        try:
            if x.role == "criterion":                 # it judges its owner's commit
                owner = self.t.t[x.parent]
                judged = owner.sha or await self.branches.head(await self.branch(owner.id))
                into = await self.branch(owner.parent) if owner.parent else None
                return replace(work, judged=judged, into=into)
            parent = await self.branch(x.parent)
            base = await self.branches.head(parent)
            if x.type.startswith("agent:") or x.type == GROUPING:
                return replace(work, branch=await self.branch(x.id), base=base)
            return replace(work, base=base)
        except GitError as e:                         # the executor's faults say it, later
            log.warning("ticket %s: %s", x.id, e)
            return work


def _report(task: asyncio.Task) -> None:
    """A task of the runner's that failed (a bug: it is logged, never lost)."""
    if not task.cancelled() and (e := task.exception()) is not None:
        log.error("ticket runner task failed", exc_info=e)
