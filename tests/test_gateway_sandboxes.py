"""Which sandboxes a change has, and which are stopped, is the gateway's word: not recorded, it
is listed on start (``openshell sandbox list``) and adopted before recovery."""
from __future__ import annotations

import asyncio
import json

from engine_harness import Flow, scenario

from legion_orch.sandbox import OpenShellRunner, SandboxError


async def _planned(f: Flow):
    """The planner reported: its sandbox is kept, stopped, while the critic works."""
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    await f.settle()
    planner = next(r for r in f.ce.runs.values() if r.role == "planner")
    assert f.runner.phase[planner.sandbox] == "Stopped"
    return planner


@scenario()
async def test_the_gateway_says_which_sandboxes_are_stopped(f: Flow):
    planner = await _planned(f)
    _, ce2 = await f.restart()
    assert ce2.runs[planner.id].sandbox_stopped and ce2.nodes["plan"].sandbox_stopped
    critic = f.run("plan", "critic")
    assert not ce2.runs[critic.id].sandbox_stopped                # running on the gateway
    # started by hand while the controller was down: it is running now
    f.runner.phase[planner.sandbox] = "Ready"
    _, ce3 = await f.restart()
    assert not ce3.runs[planner.id].sandbox_stopped and not ce3.nodes["plan"].sandbox_stopped


@scenario()
async def test_a_sandbox_gone_from_the_gateway_is_gone(f: Flow):
    planner = await _planned(f)
    del f.runner.phase[planner.sandbox]                            # deleted by hand
    _, ce2 = await f.restart()
    assert ce2.runs[planner.id].sandbox_deleted and not ce2.runs[planner.id].sandbox_stopped
    assert not ce2.nodes["plan"].sandbox_stopped
    # a partial listing (more than one page) says nothing of what it does not list
    f.runner.list_complete = False
    _, ce3 = await f.restart()
    assert not ce3.runs[planner.id].sandbox_deleted


@scenario()
async def test_a_sandbox_of_the_change_no_run_knows_is_deleted_on_start(f: Flow):
    await _planned(f)
    spec = next(iter(f.runner.specs.values()))
    f.runner.specs["lg-orphan"] = spec                             # created, never recorded
    f.runner.phase["lg-orphan"] = "Ready"
    other = type(spec)(**{**spec.__dict__, "change": "c999999-zzzz"})
    f.runner.specs["lg-theirs"] = other                            # another change's
    f.runner.phase["lg-theirs"] = "Ready"
    ctl2, ce2 = await f.restart()
    for _ in range(50):
        if "lg-orphan" in f.runner.deleted:
            break
        await asyncio.sleep(0.02)
    assert "lg-orphan" in f.runner.deleted and "lg-theirs" not in f.runner.deleted
    assert "lg-orphan is labelled for this change" in ce2.timeline()


@scenario()
async def test_a_gateway_that_does_not_answer_leaves_the_change_as_it_was(f: Flow):
    planner = await _planned(f)

    async def broken(selector=None):
        raise SandboxError("gateway unreachable")
    f.runner.list = broken
    _, ce2 = await f.restart()
    assert not ce2.runs[planner.id].sandbox_deleted               # nothing concluded


def test_the_openshell_listing_selects_the_change_and_reads_every_page(tmp_path):
    pages = {"": {"next_page_token": "abc", "sandboxes": [
                 {"name": "lg-ana", "id": "64ee", "phase": "Stopped",
                  "labels": {"legion.change": "c1", "legion.task": "build.T1"},
                  "annotations": {"legion.task": "build:T1"}}]},
             "abc": {"next_page_token": "", "sandboxes": [
                 {"name": "lg-bo", "id": "d901", "phase": "Ready",
                  "labels": {"legion.change": "c1"}}]}}
    runner = OpenShellRunner.__new__(OpenShellRunner)
    calls = []

    async def os_(*args, timeout=600):
        calls.append(args)
        token = args[args.index("--page-token") + 1] if "--page-token" in args else ""
        return json.dumps(pages[token])
    runner._os = os_
    listing = asyncio.run(runner.list({"legion.change": "c1"}))
    assert calls == [
        ("sandbox", "list", "--output", "json", "--selector", "legion.change=c1"),
        ("sandbox", "list", "--output", "json", "--selector", "legion.change=c1",
         "--page-token", "abc")]
    assert listing.complete
    ana, bo = listing.sandboxes
    assert (ana.name, ana.id, ana.change, ana.stopped) == ("lg-ana", "64ee", "c1", True)
    assert ana.annotations["legion.task"] == "build:T1" and not bo.stopped
    pages[""]["next_page_token"] = pages["abc"]["next_page_token"] = "abc"   # never ends
    runner.MAX_PAGES = 3
    assert not asyncio.run(runner.list()).complete
