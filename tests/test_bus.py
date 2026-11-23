"""The event bus (bus.py): notifications between the controller's components."""
from __future__ import annotations

import asyncio
import inspect

import pytest
from engine_harness import Flow, scenario

from legion_orch import bus as bus_mod
from legion_orch.bus import Bus, CardsPublished, Escalated, Event, ItemAdded


@pytest.mark.anyio
async def test_scoping_order_failures_and_coroutine_handlers():
    spawned = []
    b = Bus(lambda coro: spawned.append(asyncio.ensure_future(coro)))
    seen = []
    b.subscribe(ItemAdded, lambda e: seen.append(("any", e.card)))
    b.subscribe(ItemAdded, lambda e: seen.append(("c1", e.card)), cid="c1")
    b.subscribe(ItemAdded, lambda e: 1 / 0)                         # a failing subscriber

    async def later(e):
        seen.append(("async", e.card))
    b.subscribe(ItemAdded, later)
    b.publish(ItemAdded("c1", "a@x"))
    b.publish(ItemAdded("c2", "b@y"))                           # not c1's
    assert seen == [("any", "a@x"), ("c1", "a@x"), ("any", "b@y")]   # in order, at once
    await asyncio.gather(*spawned)
    assert ("async", "a@x") in seen and ("async", "b@y") in seen
    b.drop("c1")
    seen.clear()
    b.publish(ItemAdded("c1", "c@z"))
    assert ("c1", "c@z") not in seen and ("any", "c@z") in seen


@scenario()
async def test_a_changes_events_wait_for_its_save(f: Flow):
    seen = []
    f.ctl.bus.subscribe(Escalated, lambda e: seen.append(e.node))
    async with f.ce.lock:
        f.ce.emit(Escalated(f.ce.cid, "x"))
        assert seen == []                      # raised under the lock: not yet
        f.ce.save()
    assert seen == ["x"]                       # published once it saved


@scenario()
async def test_every_event_has_a_subscriber_in_a_running_controller(f: Flow):
    """With the A2A layer up, no event is published into the void."""
    from legion_orch.server import A2AHub
    A2AHub.of(f.ctl)
    kinds = [k for _, k in inspect.getmembers(bus_mod, inspect.isclass)
             if issubclass(k, Event) and k is not Event]
    assert {k.__name__ for k in kinds} >= {"CardsPublished", "CardsWithdrawn", "ItemAdded",
                                           "TurnEnded", "Escalated", "Pushed"}
    missing = [k.__name__ for k in kinds if not f.ctl.bus.subscribers(k)]
    assert missing == [], missing
    assert f.ctl.bus.subscribers(CardsPublished) >= 1
