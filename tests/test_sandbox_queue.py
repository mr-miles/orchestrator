"""The sandbox queue (engine/sandbox_queue.py): limits.max_parallel across every change, used
through the dispatcher – verifiers first, then first come; a request holds its place at once."""
from __future__ import annotations

import asyncio

from engine_harness import Flow, scenario

from legion_orch.workflow.engine.sandbox_queue import AGENT, VERIFIER, SandboxQueue, take
from legion_orch.workflow.model import R_FAILED


def run(coro):
    return asyncio.run(coro)


def test_requests_are_granted_up_to_the_limit_verifiers_first():
    async def go():
        q = SandboxQueue(1)
        first = q.request(AGENT)
        assert first.done() and q.in_use == 1               # free: granted at once
        agent, verifier = q.request(AGENT), q.request(VERIFIER)
        assert not agent.done() and not verifier.done() and q.waiting == 2
        first.result().release()
        assert verifier.done() and not agent.done()          # work in flight goes first
        verifier.result().release()
        assert agent.done() and q.in_use == 1
    run(go())


def test_a_place_is_held_from_the_request_and_try_take_waits_behind_it():
    async def go():
        q = SandboxQueue(1)
        held = q.request(AGENT).result()
        waiting = q.request(VERIFIER)
        held.release()
        assert waiting.done()                               # granted, not taken by a newcomer
        assert q.try_take(AGENT) is None
        waiting.result().release()
        assert q.try_take(AGENT) is not None
    run(go())


def test_a_cancelled_request_gives_up_its_place_and_a_slot_is_released_once():
    async def go():
        q = SandboxQueue(1)
        held = q.request(AGENT).result()
        gone, next_ = q.request(VERIFIER), q.request(AGENT)
        task = asyncio.ensure_future(take(gone))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        held.release()
        held.release()                                      # twice: once
        assert next_.done() and q.in_use == 1
    run(go())


@scenario(max_parallel=1)
async def test_a_verification_waits_for_room_within_the_limit(f: Flow):
    """A verifier's sandbox counts against limits.max_parallel with the agent runs: with the
    one sandbox in use by an agent, a verification waits until it is returned."""
    from legion_orch.workflow.engine.phases import Verify
    run_ = f.run("intake")                                   # holds the one sandbox
    task = asyncio.ensure_future(Verify(("pytest",)).verification(f.ce._phases, "x", "a" * 40))
    await asyncio.sleep(0.1)
    assert not task.done() and f.ctl.svc.queue.waiting == 1
    async with f.ce.lock:
        run_.status = R_FAILED
        f.ce._dispatcher._release(run_.id)                  # its sandbox is done
    res = await asyncio.wait_for(task, 5)
    assert res.ok and f.ctl.svc.queue.in_use == 0
