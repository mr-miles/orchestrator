"""The sandbox layer on its own (engine/sandboxes.py): which sandboxes nodes hold, how an agent's
sandbox is created, prepared and paused, the verifier sandbox's clean-up, and retirement. Driven
directly against the fake runner, without the scheduler."""
from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from engine_harness import Flow, scenario

from legion_orch.sandbox import SandboxError
from legion_orch.workflow.engine.sandboxes import Sandboxes
from legion_orch.workflow.model import (
    ACCEPTED,
    BUILD,
    H_REQUIREMENTS,
    INTAKE,
    R_FAILED,
    R_REPORTED,
    ChangeStatus,
    Node,
    Run,
    SetupPin,
)

SHA = "a" * 40
JUDGED = "b" * 40


def sb(f: Flow) -> Sandboxes:
    return f.ce._sandboxes


def make_run(f: Flow, role: str, *, node: str = "T9", sandbox: str = "sb-t9", **kw) -> Run:
    run = Run(id=f"r{9000 + len(f.ce.runs)}", node_id=node, role=role, soul="s", cycle=1,
              base_sha=SHA, sandbox=sandbox, **kw)
    f.ce.runs[run.id] = run
    return run


def build_node(f: Flow, *, sandbox: str = "sb-t9", **kw) -> Node:
    node = Node(id="T9", kind=BUILD, title="a task", sandbox=sandbox, **kw)
    f.ce.nodes[node.id] = node
    return node


def events(f: Flow) -> list[str]:
    return [json.loads(e)["message"] for e in f.ce._timeline]


def scripts(steps) -> list[list[str]]:
    return [[argv[1].rsplit("/", 1)[-1], *argv[2:]] for _, argv, _ in steps]


# ---------------------------------------------------------------------------- holds
@scenario()
async def test_a_kept_session_is_held_until_its_gate_passes(f: Flow):
    intake = next(n for n in f.ce.nodes.values() if n.kind == INTAKE)
    assert intake.sandbox and sb(f).holds(intake)
    assert intake.sandbox in sb(f).held()
    f.ce.nodes[H_REQUIREMENTS].status = ACCEPTED
    assert not sb(f).holds(intake)


@scenario()
async def test_the_implementer_holds_its_sandbox_until_its_node_is_accepted(f: Flow):
    node = build_node(f)
    assert sb(f).holds(node)
    node.status = ACCEPTED
    assert not sb(f).holds(node)


@scenario()
async def test_nothing_is_held_without_a_sandbox_or_once_the_change_ended(f: Flow):
    node = build_node(f, sandbox=None)
    assert not sb(f).holds(node)
    node.sandbox = "sb-t9"
    f.ce.change.status = ChangeStatus.ABANDONED
    assert not sb(f).holds(node)
    assert sb(f).held() == set()


# ---------------------------------------------------------------------------- preparation
@scenario()
async def test_a_fresh_sandbox_clones_then_checks_out_detached(f: Flow):
    steps = await sb(f).prepare_steps(make_run(f, "intake"), fresh=True)
    assert scripts(steps) == [["clone.sh"], ["checkout.sh", SHA]]
    assert all(required for *_, required in steps)


@scenario()
async def test_a_reused_sandbox_checks_out_the_judged_commit_without_cloning(f: Flow):
    run = make_run(f, "reviewer", judged_sha=JUDGED)
    assert scripts(await sb(f).prepare_steps(run, fresh=False)) == [["checkout.sh", JUDGED]]


@scenario()
async def test_a_pushing_role_works_on_its_branch_from_its_base_or_hub_tip(f: Flow):
    run = make_run(f, "implementer", branch="legion/t9")
    assert scripts(await sb(f).prepare_steps(run, fresh=False)) == \
        [["branch.sh", "legion/t9", SHA]]
    tip = f.ce.change.integration_head
    await f.ce.git.git("update-ref", "refs/heads/legion/t9", tip)
    assert scripts(await sb(f).prepare_steps(run, fresh=False)) == \
        [["branch.sh", "legion/t9", tip]]


@scenario()
async def test_setup_runs_only_with_a_pin_for_roles_that_use_a_commit(f: Flow):
    f.ce.change.setup_pin = SetupPin(blob="c" * 40, source="base")
    impl = await sb(f).prepare_steps(make_run(f, "implementer", branch="legion/t9"), fresh=True)
    assert scripts(impl)[-1] == ["mise-setup.sh", "c" * 40]
    assert impl[-1][2] is False                       # a failed setup does not stop the launch
    intake = await sb(f).prepare_steps(make_run(f, "intake"), fresh=True)   # git: none
    assert "mise-setup.sh" not in [s[0] for s in scripts(intake)]
    f.ce.change.blank_repo = True                     # until the scaffold is accepted
    impl = await sb(f).prepare_steps(make_run(f, "implementer", branch="legion/t9"), fresh=True)
    assert "mise-setup.sh" not in [s[0] for s in scripts(impl)]


@scenario()
async def test_a_role_without_the_repository_is_not_prepared(f: Flow):
    roles = f.ce.assets.roles
    roles["intake"] = dataclasses.replace(roles["intake"], repo=False)
    assert await sb(f).prepare_steps(make_run(f, "intake"), fresh=True) == []


@scenario()
async def test_a_failed_checkout_fails_the_preparation(f: Flow):
    log = f.tmp / "agent.log"
    f.runner.setup_rc = 1
    with pytest.raises(SandboxError, match="clone the repository failed"):
        await sb(f).prepare(make_run(f, "intake"), log, fresh=True)
    assert "clone the repository: exit 1" in log.read_text()


@scenario()
async def test_a_failed_setup_is_logged_and_the_preparation_goes_on(f: Flow):
    f.ce.change.setup_pin = SetupPin(blob="c" * 40, source="base")
    f.runner.commands["c" * 40] = (1, "mise: no such task\n")
    log = f.tmp / "agent.log"
    await sb(f).prepare(make_run(f, "implementer", branch="legion/t9"), log, fresh=True)
    text = log.read_text()
    assert "check out branch legion/t9" in text and "mise: no such task" in text
    assert "the working copy may lack its dependencies" in text


# ---------------------------------------------------------------------------- creation
@scenario()
async def test_a_name_clash_takes_a_new_name_and_moves_the_node_with_it(f: Flow):
    node, run = build_node(f), make_run(f, "implementer")
    f.runner.clash = 1
    payload, policy = f.tmp / "payload", f.tmp / "policy.yaml"
    payload.mkdir()
    policy.write_text("{}\n")
    name = await sb(f).create(run, payload, policy, push=True)
    assert name == run.sandbox != "sb-t9"
    assert node.sandbox == run.sandbox
    assert f.runner.specs[name].push and f.runner.specs[name].role == "implementer"


@scenario()
async def test_creation_gives_up_after_its_attempts_and_other_errors_raise_at_once(f: Flow):
    build_node(f)
    payload, policy = f.tmp / "payload", f.tmp / "policy.yaml"
    payload.mkdir()
    policy.write_text("{}\n")
    f.runner.clash = 3
    with pytest.raises(SandboxError, match="already exists"):
        await sb(f).create(make_run(f, "implementer"), payload, policy, push=True)
    f.runner.fail_create = "implementer"
    with pytest.raises(SandboxError, match="quota"):
        await sb(f).create(make_run(f, "implementer"), payload, policy, push=True,
                           attempts=1)


# ---------------------------------------------------------------------------- pausing
@scenario()
async def test_a_held_idle_sandbox_is_stopped_once(f: Flow):
    node, run = build_node(f), make_run(f, "implementer", status=R_REPORTED)
    await sb(f).pause(run)
    await sb(f).pause(run)
    assert f.runner.stopped == ["sb-t9"] and node.sandbox_stopped


@scenario()
async def test_a_sandbox_is_not_stopped_while_in_use_or_not_held(f: Flow):
    node = build_node(f)
    live = make_run(f, "implementer")                      # still starting
    await sb(f).pause(live)
    live.status, node.status = R_REPORTED, ACCEPTED       # done, but no longer held
    await sb(f).pause(live)
    assert f.runner.stopped == [] and not node.sandbox_stopped


@scenario()
async def test_a_sandbox_that_cannot_stop_stays_running(f: Flow):
    node, run = build_node(f), make_run(f, "implementer", status=R_REPORTED)
    f.runner.fail_stop = True
    await sb(f).pause(run)
    assert not node.sandbox_stopped
    assert any("could not stop sb-t9" in e for e in events(f))


# ---------------------------------------------------------------------------- verifiers
def grants(f: Flow) -> dict:
    return f.ctl.svc.auth._verifiers


@scenario()
async def test_a_verifier_is_checked_out_and_removed_afterwards(f: Flow):
    async with sb(f).verifier("v-1", "T1", JUDGED) as box:
        assert grants(f) and box.workdir.endswith("/work")
        assert [a[1].rsplit("/", 1)[-1] for n, a in f.runner.execs if n == "v-1"] == \
            ["clone.sh", "checkout.sh"]
        r = await box.run("pytest -q")
        assert r.command == "pytest -q" and r.exit_code == 0
    assert f.runner.deleted == ["v-1"] and not grants(f)
    assert not (f.ctl.svc.tmp / "verify" / "v-1").exists()


@scenario()
async def test_a_verifier_is_removed_when_its_checkout_or_its_body_fails(f: Flow):
    f.runner.setup_rc = 1
    with pytest.raises(SandboxError, match="verifier clone.sh failed"):
        async with sb(f).verifier("v-1", "T1", JUDGED):
            pass
    f.runner.setup_rc = 0
    with pytest.raises(RuntimeError):
        async with sb(f).verifier("v-2", "T1", JUDGED):
            raise RuntimeError("boom")
    assert f.runner.deleted == ["v-1", "v-2"] and not grants(f)


@scenario()
async def test_a_verifier_that_was_never_created_is_not_deleted(f: Flow):
    f.runner.fail_create = "verifier"
    with pytest.raises(SandboxError):
        async with sb(f).verifier("v-1", "T1", JUDGED):
            pass
    assert f.runner.deleted == [] and not (f.ctl.svc.tmp / "verify" / "v-1").exists()


# ---------------------------------------------------------------------------- retiring
@scenario()
async def test_a_retired_sandbox_is_reflected_on_then_deleted(f: Flow):
    run = make_run(f, "implementer", status=R_REPORTED)
    order = []
    real = sb(f).reflector.reflect

    async def reflect(name, stopped=False):
        order.append(("reflect", name, stopped, name in sb(f).reflector.pending))
        await real(name, stopped)
    sb(f).reflector.reflect = reflect
    sb(f).delete_later("sb-t9", stopped=True)
    assert "sb-t9" in sb(f).retiring
    await f.settle()
    assert order == [("reflect", "sb-t9", True, True)]
    assert f.runner.deleted == ["sb-t9"] and run.sandbox_deleted
    assert not sb(f).retiring and not sb(f).reflector.pending


@scenario()
async def test_a_failed_reflection_still_clears_the_retirement(f: Flow):
    async def reflect(name, stopped=False):
        raise RuntimeError("no session")
    sb(f).reflector.reflect = reflect
    sb(f).delete_later("sb-t9")
    await asyncio.sleep(0.05)
    assert not sb(f).retiring and not sb(f).reflector.pending


@scenario()
async def test_a_refused_delete_is_reported_and_not_recorded(f: Flow):
    run = make_run(f, "implementer", status=R_REPORTED)
    f.runner.fail_delete = True
    assert await sb(f).delete("sb-t9") is False
    assert not run.sandbox_deleted
    assert any("delete of sb-t9 failed" in e for e in events(f))


@scenario(max_parallel=1)
async def test_a_verifier_waits_for_room_within_the_limit(f: Flow):
    """Verifier sandboxes count against limits.max_parallel with the agent runs: with the one
    sandbox allowed in use by an agent, a verification waits until it is free."""
    run = f.run("intake")                                     # the one sandbox allowed
    assert not sb(f).has_room()
    entered = asyncio.Event()

    async def verify():
        async with sb(f).verifier("v-1", "T1", JUDGED):
            entered.set()
    task = asyncio.create_task(verify())
    await asyncio.sleep(0.1)
    assert not entered.is_set() and "v-1" not in f.runner.created
    async with f.ce.lock:
        run.status = R_FAILED                                 # its sandbox is free
        f.ce.save()
    await asyncio.wait_for(task, 5)
    assert entered.is_set() and f.runner.deleted[-1] == "v-1"
    assert f.ctl.sandboxes_in_use() == 0
