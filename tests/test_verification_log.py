"""A verification's log, like an agent run's: written as each command starts and ends, shown
live (the change's state names it), then stored and served by ``/api/runs/<log>/log``."""
from __future__ import annotations

import asyncio

import httpx
from engine_harness import Flow, scenario

from legion_orch.server.views import change_state
from legion_orch.server.web import build_app


@scenario()
async def test_a_verification_has_a_live_then_stored_log_served_like_a_run(f: Flow):
    await f.to_build()
    gate = asyncio.Event()
    real_exec = f.runner.exec

    async def exec_(name, argv, **kw):
        if argv[-1] == "grep -q hello src/greet.txt":       # T1's command: held while we look
            await gate.wait()
            return 0, "found hello\n"
        return await real_exec(name, argv, **kw)
    f.runner.exec = exec_

    async def wait_for(cond):
        for _ in range(250):
            if cond():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("timed out")

    report = asyncio.create_task(f.implement())
    await wait_for(lambda: "verify:T1" in f.ce.verifying)
    log = f.ce.verifying["verify:T1"]
    await wait_for(lambda: "grep -q hello" in f.ce.log_tail(log))
    assert change_state(f.ctl, f.ce)["verifying"] == {"verify:T1": log}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(f.ctl)),
                                 base_url="http://op") as c:
        live = (await c.get(f"/api/runs/{log}/log", params={"format": "entries"})).json()
        assert live["live"] and live["stage"] == "VERIFYING"
        text = "\n".join(e["text"] for e in live["entries"])
        assert "checkout.sh: exit 0" in text and "grep -q hello src/greet.txt …" in text

        gate.set()
        await report
        await f.settle()
        assert f.status("verify:T1") == "passed" and "verify:T1" not in f.ce.verifying
        assert f.status("review:T1") == "working"
        assert f.ce.latest(f"control/runs/{log}/log")                    # stored
        done = (await c.get(f"/api/runs/{log}/log", params={"format": "entries"})).json()
        assert not done["live"] and done["status"] == "ACCEPTED"
        text = "\n".join(e["text"] for e in done["entries"])
        assert "found hello" in text and "grep -q hello src/greet.txt: exit 0" in text
        assert "verification passed" in text
        assert (await c.get("/api/runs/lgv-nope/log")).status_code == 400
