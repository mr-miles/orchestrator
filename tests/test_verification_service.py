"""Verification as a service (engine/services/verification.py): a commit's commands are verified
once per key – the commit, the commands, the setup pin, the verifier image and the timeout – as
it is pushed; the Verify phase, agents (MCP) and operators get that result, and judges are only
dispatched on a passing one (G-V0)."""
from __future__ import annotations

import asyncio

import pytest
from engine_harness import TASKS, Flow, scenario

from legion_orch.git import GitGrant
from legion_orch.workflow.engine import WorkflowError
from legion_orch.workflow.engine.base.dispatch import PriorityCap
from legion_orch.workflow.model import SetupPin, VerificationState

CMD = TASKS[0]["verification"][0]


def verifiers(f: Flow) -> list[str]:
    return [s for s in f.runner.created if s.startswith("lgv")]


async def push(f: Flow, files: dict[str, str] | None = None) -> tuple[object, str]:
    """T1's implementer commits and pushes; the git remote tells the controller."""
    r = f.run("build:T1", "implementer")
    sha = f.commit(r, files or {"src/greet.txt": "hello\n"})
    await f.ctl.git_pushed(GitGrant(r.id, f"refs/heads/{r.branch}", "app"))
    return r, sha


async def report(f: Flow, r, sha: str):
    ev = await f.write(r, "tasks/T1/evidence/cycle-1.md", f"- `{CMD}` -> exit 0")
    return await f.report(r, [f"git:app@{sha}", ev])


@scenario()
async def test_a_pushed_tip_is_verified_once_and_the_report_uses_it(f: Flow):
    await f.to_build()
    r, sha = await push(f)
    await f.settle()
    [v] = f.ce.verifications(sha)
    assert v.state == VerificationState.PASSED and v.requested_by == [f"push:{r.id}"]
    assert len(verifiers(f)) == 1
    await f.ctl.git_pushed(GitGrant(r.id, f"refs/heads/{r.branch}", "app"))   # the same tip
    await report(f, r, sha)
    assert len(verifiers(f)) == 1                       # the Verify phase: the push's result
    rec = f.ce.nodes["build:T1"].verification["1"]
    assert rec.ok and rec.cached and rec.run == v.id
    assert f.status("build:T1") == "REVIEWING"
    assert "step:build:T1" in f.ce.change.verifications[v.id].requested_by


@scenario()
async def test_the_key_holds_everything_that_decides_the_outcome(f: Flow):
    await f.to_build()
    svc = f.ce._verifications
    base = svc.key("a" * 40, ("pytest",))
    assert svc.key("a" * 40, ("pytest",)).hash == base.hash
    others = [svc.key("b" * 40, ("pytest",)), svc.key("a" * 40, ("pytest", "ruff"))]
    f.ce.change.setup_pin = SetupPin(blob="c" * 40, source="base")
    others.append(svc.key("a" * 40, ("pytest",)))
    f.ce.change.setup_pin = None
    f.cfg.limits.verify_timeout_s += 1
    others.append(svc.key("a" * 40, ("pytest",)))
    f.cfg.limits.verify_timeout_s -= 1
    f.cfg.sandbox.verifier_image = "reg/other:1"
    others.append(svc.key("a" * 40, ("pytest",)))
    assert len({k.hash for k in others} | {base.hash}) == 6


@scenario()
async def test_an_infrastructure_error_is_not_a_result_and_a_human_may_run_one_again(f: Flow):
    await f.to_build()
    svc = f.ce._verifications
    key = svc.key("a" * 40, ("pytest",))
    f.runner.fail_create = "verifier"
    first = await svc.verify(key, "test", 0)
    assert first.state == VerificationState.INFRA_ERROR and svc.result(key) is None
    f.runner.fail_create = None
    second = await svc.verify(key, "test", 0)            # asked again: it runs again
    assert second.state == VerificationState.PASSED and second.attempt == 2
    assert (await svc.verify(key, "test", 0)).id == second.id      # final: reused
    with pytest.raises(WorkflowError, match="decision owner"):
        await f.ce.reverify(second.id, "mallory")
    third = await f.ce.reverify(second.id, "miles")       # challenged: a new attempt
    await f.settle()
    assert f.ce.change.verifications[third.id].attempt == 3
    assert f.ce.change.verifications[third.id].requested_by == ["operator:miles"]


@scenario(max_parallel_verifications=1)
async def test_a_newer_push_cancels_the_older_tips_verification_that_has_not_started(f: Flow):
    await f.to_build()
    gate = asyncio.Event()
    real = f.runner.exec

    async def exec_(name, argv, **kw):
        if argv[-1] == CMD:
            await gate.wait()
        return await real(name, argv, **kw)
    f.runner.exec = exec_
    f.ctl.svc.verify_cap = PriorityCap(1)
    f.ce._verifications.cap = f.ctl.svc.verify_cap
    _, s1 = await push(f, {"src/greet.txt": "hello 1\n"})
    for _ in range(100):                                  # the first is running
        if f.ce.verifications(s1)[0].state == VerificationState.RUNNING:
            break
        await asyncio.sleep(0.02)
    _, s2 = await push(f, {"src/greet.txt": "hello 2\n"})
    _, s3 = await push(f, {"src/greet.txt": "hello 3\n"})
    assert f.ce.verifications(s2)[0].state == VerificationState.CANCELLED
    gate.set()
    await f.settle()
    assert f.ce.verifications(s1)[0].state == VerificationState.PASSED      # left to finish
    assert f.ce.verifications(s3)[0].state == VerificationState.PASSED
    assert len(verifiers(f)) == 2


def test_the_cap_serves_by_priority_then_in_order():
    async def go():
        cap, order = PriorityCap(1), []
        await cap.acquire(0)                              # held

        async def take(p, name):
            await cap.acquire(p)
            order.append(name)
            cap.release()
        tasks = [asyncio.ensure_future(take(p, n)) for p, n in
                 ((3, "agent"), (2, "push-1"), (0, "step"), (2, "push-2"))]
        await asyncio.sleep(0)
        cap.release()
        await asyncio.gather(*tasks)
        assert order == ["step", "push-1", "push-2", "agent"]
    asyncio.run(go())


@scenario()
async def test_judges_are_dispatched_only_on_a_verified_candidate(f: Flow):
    await f.to_build()
    r, sha = await push(f)
    await report(f, r, sha)
    node = f.ce.nodes["build:T1"]
    agents = f.ce._agents
    assert agents.preconditions(node, "reviewer") is None          # verified: judged
    f.ce.change.setup_pin = SetupPin(blob="d" * 40, source="base")   # the key moves on
    esc = agents.preconditions(node, "reviewer")
    assert esc is not None and esc.kind == "unverified" and "G-V0" in esc.question


@scenario()
async def test_an_agent_asks_for_its_commands_within_its_limit(f: Flow):
    f.cfg.limits.verification_requests_per_run = 1
    await f.to_build()
    r = f.run("build:T1", "implementer")
    sha = f.commit(r, {"src/greet.txt": "hello\n"})
    v = await f.ce.request_verification(r, sha)
    await f.settle()
    v = f.ce.change.verifications[v.id]
    assert v.state == VerificationState.PASSED and v.requested_by == [f"agent:{r.id}"]
    assert f.ce.latest_verification(r, sha).id == v.id
    assert (await f.ce.request_verification(r, sha)).id == v.id    # its result: no new run
    sha2 = f.commit(r, {"src/greet.txt": "hello again\n"})
    with pytest.raises(WorkflowError, match="verification_requests_per_run"):
        await f.ce.request_verification(r, sha2)
    record = f.ce.verification_record(v.id)
    assert record["results"][-1]["command"] == CMD and record["results"][-1]["exit_code"] == 0
    planner_like = next(x for x in f.ce.runs.values() if x.role == "planner")
    with pytest.raises(WorkflowError, match="only build tasks"):
        f.ce.verification_key_for(planner_like, sha)


@scenario()
async def test_a_restart_forgets_verifications_in_flight(f: Flow):
    await f.to_build()
    svc = f.ce._verifications
    gate = asyncio.Event()
    real = f.runner.exec

    async def exec_(name, argv, **kw):
        if argv[-1] == "slow":
            await gate.wait()
        return await real(name, argv, **kw)
    f.runner.exec = exec_
    async with f.ce.lock:
        v = svc.request(svc.key("a" * 40, ("slow",)), "test", 0)
    await asyncio.sleep(0.1)
    svc.recover()
    assert v.state == VerificationState.INFRA_ERROR and "restarted" in v.result.infra_error
    gate.set()
