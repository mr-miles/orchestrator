"""The dispatcher on its own (engine/dispatch.py): opaque work within the limit, step
reservations – a slot from the first work, kept while work follows straight on, given back
while the step waits, released when it is over – and first come, first served."""
from __future__ import annotations

import asyncio
import functools

import pytest

from legion_orch.workflow.engine.base.dispatch import Dispatcher, Withdrawn


class Job:
    """Work that runs until the test lets it finish."""

    def __init__(self, name: str, log: list[str], ready=lambda: True):
        self.name, self.log, self.done, self.ready = name, log, asyncio.Event(), ready

    def can_start(self) -> bool:
        return self.ready()

    async def execute(self) -> str:
        self.log.append(self.name)
        await self.done.wait()
        return self.name


def sync(fn):
    """Run the async test in its own event loop."""
    @functools.wraps(fn)
    def wrapper():
        asyncio.run(fn())
    return wrapper


async def tick() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@sync
async def test_work_runs_within_the_limit_first_come_first_served():
    d, log = Dispatcher(1), []
    a, b, c = (Job(n, log) for n in "abc")
    ta, tb, tc = (asyncio.ensure_future(d.run(j)) for j in (a, b, c))
    await tick()
    assert log == ["a"] and d.in_use == 1 and d.waiting == 2
    a.done.set()
    assert await ta == "a"
    await tick()
    assert log == ["a", "b"]
    b.done.set()
    c.done.set()
    assert (await tb, await tc) == ("b", "c") and d.in_use == 0


@sync
async def test_a_steps_next_work_slots_straight_into_its_reserved_slot():
    d, log = Dispatcher(1), []
    r = d.reserve()
    assert not r.bound and d.in_use == 0                 # no slot until its first work
    agent, other, verify = Job("agent", log), Job("other", log), Job("verify", log)
    t1 = asyncio.ensure_future(d.run(agent, r))
    await tick()
    t2 = asyncio.ensure_future(d.run(other))             # another step's new work queues
    await tick()
    agent.done.set()
    await t1
    t3 = asyncio.ensure_future(d.run(verify, r))         # follows straight on
    await tick()
    assert log == ["agent", "verify"] and r.bound
    verify.done.set()
    await t3
    r.release()                                           # the step is over: the slot goes
    await tick()
    assert log[-1] == "other"
    other.done.set()
    await t2
    assert d.in_use == 0


@sync
async def test_parallel_work_of_a_step_takes_free_slots_or_the_reserved_one():
    d, log = Dispatcher(2), []
    r = d.reserve()
    j1, j2, j3 = (Job(f"judge{i}", log) for i in (1, 2, 3))
    ts = [asyncio.ensure_future(d.run(j, r)) for j in (j1, j2, j3)]
    await tick()
    assert log == ["judge1", "judge2"] and d.in_use == 2  # its slot and a free one
    j1.done.set()                                         # its reserved slot frees
    await ts[0]
    await tick()
    assert log[-1] == "judge3"
    j2.done.set()
    j3.done.set()
    await asyncio.gather(*ts)
    assert d.in_use == 1 and r.bound                      # still reserved, idle
    r.release()
    assert d.in_use == 0


@sync
async def test_a_waiting_step_gives_its_slot_back_until_its_next_work():
    d, log = Dispatcher(1), []
    r = d.reserve()
    first, setup = Job("first", log), Job("setup-check", log)
    t = asyncio.ensure_future(d.run(first, r))
    await tick()
    first.done.set()
    await t
    blocked = asyncio.ensure_future(d.run(setup))         # unreserved: no free slot
    await tick()
    assert log == ["first"]
    d.idle(r)                                             # the step waits on its conditions
    await tick()
    assert log == ["first", "setup-check"] and not r.bound
    setup.done.set()
    await blocked
    again = Job("again", log)
    t = asyncio.ensure_future(d.run(again, r))            # binds a slot again
    await tick()
    assert r.bound and log[-1] == "again"
    again.done.set()
    await t


@sync
async def test_releasing_withdraws_the_steps_waiting_work():
    d, log = Dispatcher(1), []
    busy, r = Job("busy", log), d.reserve()
    t1 = asyncio.ensure_future(d.run(busy))
    await tick()
    t2 = asyncio.ensure_future(d.run(Job("queued", log), r))
    await tick()
    r.release()
    with pytest.raises(Withdrawn):
        await t2
    with pytest.raises(Withdrawn):
        await d.run(Job("late", log), r)
    busy.done.set()
    await t1
    assert log == ["busy"] and d.in_use == 0 and d.waiting == 0


@sync
async def test_a_reserved_slot_goes_back_when_its_work_completes_after_release():
    d, log = Dispatcher(1), []
    r = d.reserve()
    j = Job("j", log)
    t = asyncio.ensure_future(d.run(j, r))
    await tick()
    r.release()                                           # its work still runs
    assert d.in_use == 1
    j.done.set()
    await t
    assert d.in_use == 0


@sync
async def test_cancelled_waiting_work_gives_up_its_place():
    d, log = Dispatcher(1), []
    a = Job("a", log)
    ta = asyncio.ensure_future(d.run(a))
    await tick()
    tb = asyncio.ensure_future(d.run(Job("b", log)))
    await tick()
    tb.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tb
    assert d.waiting == 0
    a.done.set()
    await ta
    assert d.in_use == 0 and log == ["a"]


@sync
async def test_work_that_cannot_start_keeps_its_place_and_holds_no_slot():
    """Work waiting for its environment is passed over, its step's idle reserved slot goes back
    meanwhile, and it starts at the next change of slots or reservations once it can – as
    provisioning ends with its step's reservation released."""
    d, log, built = Dispatcher(1), [], [False]
    step, provision = d.reserve(), d.reserve()
    first = Job("first", log)
    t1 = asyncio.ensure_future(d.run(first, step))
    await tick()
    first.done.set()
    await t1                                          # the step holds its slot, idle
    judge = Job("judge", log, ready=lambda: built[0])
    t2 = asyncio.ensure_future(d.run(judge, step))    # waits for the environment
    await tick()
    assert log == ["first"] and not step.bound and d.in_use == 0      # its slot went back
    check = Job("setup check", log)                   # provisioning's own work: it can start
    t3 = asyncio.ensure_future(d.run(check, provision))
    await tick()
    assert log == ["first", "setup check"]
    built[0] = True                                   # provisioning recorded as accepted …
    check.done.set()
    await t3
    provision.release()                               # … then its reservation goes back
    await tick()
    assert log == ["first", "setup check", "judge"]
    judge.done.set()
    assert await t2 == "judge"
