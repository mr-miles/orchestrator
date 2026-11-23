"""The dispatcher on its own (engine/base/dispatch.py): opaque work within the limit, first
come, first served; work that cannot start keeps its place and holds no slot."""
from __future__ import annotations

import asyncio
import functools

import pytest

from legion_orch.workflow.engine.base.dispatch import Dispatcher


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
    """Work waiting for its environment is passed over – later work that can start goes first –
    and starts once it can, when the dispatcher looks again (a change saves: the build is done)."""
    d, log, built = Dispatcher(1), [], [False]
    judge = Job("judge", log, ready=lambda: built[0])
    t1 = asyncio.ensure_future(d.run(judge))            # waits for the environment
    await tick()
    assert log == [] and d.in_use == 0 and d.waiting == 1
    other = Job("other", log)
    t2 = asyncio.ensure_future(d.run(other))           # can start: takes the free slot
    await tick()
    assert log == ["other"]
    other.done.set()
    await t2
    await tick()
    assert log == ["other"] and d.in_use == 0          # still not built: it keeps waiting
    built[0] = True
    d.recheck()                                        # a save: the build is recorded
    await tick()
    assert log == ["other", "judge"]
    judge.done.set()
    assert await t1 == "judge" and d.in_use == 0
