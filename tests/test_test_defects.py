"""A test the oracle confirms is wrong (WORKFLOW.md §5.4): asked over A2A, the oracle calls
``test_defect``; its fix – its next instance, in its own session – is added at the end of
delivery once nothing runs, its revised tests are signed off again, and delivery's criteria
judge again after it."""
from __future__ import annotations

import pytest
from engine_harness import Flow, scenario, signals
from test_a2a import client, consult_answers, rpc, send, task_when, token

from legion_orch.workflow.engine import WorkflowError

R2 = signals(scope_breadth=2, novelty_uncertainty=2, verification_quality=2)
TEST = "tests/acceptance/test_greet.sh"
WHY = "it greps for 'Hello'; REQ-1 says the greeting is lowercase 'hello'"


@scenario()
async def test_a_defect_the_oracle_confirms_is_fixed_in_its_session_and_signed_off_again(f: Flow):
    await f.to_build(R2)
    oracle = f.runs("oracle", "oracle")[-1]
    oracle.session_id = "session-of-the-oracle"             # what its log said
    impl = f.run("T1", "implementer")
    with pytest.raises(WorkflowError, match="only the oracle"):
        await f.ce.test_defect(impl, [TEST], WHY)
    async with client(f) as c:
        res = await rpc(c, "oracle@oracle", send(f"Is {TEST} right? {WHY}"), token(f, impl))
        for _ in range(250):                       # its answer turn, in its kept sandbox
            turns = [r for r in f.ce.runs.values() if r.consult and r.role == "oracle"
                     and r.id in f.ctl.svc.procs]
            if turns:
                break
            await f.settle()
        [turn] = turns
        assert turn.sandbox == oracle.sandbox
        with pytest.raises(WorkflowError, match="not the oracle's tests"):
            await f.ce.test_defect(turn, ["src/greet.txt"], WHY)
        out = await f.ce.test_defect(turn, [TEST], WHY)
        assert out == {"fix": "oracle.2", "queued": False}
        await consult_answers(f, "oracle", "oracle", "You are right: a fix follows.",
                              f"session-{oracle.id}")
        t = await task_when(c, "oracle@oracle", res["task"]["id"], token(f, impl))
        assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "replan" in f.ce.tickets.root.paused                    # nothing new starts meanwhile
    await f.implement("T1")                                    # the build drains
    await f.settle()
    fix = f.ticket("oracle.2")                                 # applied once quiet
    delivery = f.ticket("delivery")
    assert fix.parent == "delivery" and delivery.subtasks[-1] == ["oracle.2"]
    assert fix.keep and any(WHY in x for x in fix.findings)
    assert fix.criteria == ["preflight.2", "H-2.2"]
    assert "replan" not in f.ce.tickets.root.paused
    if "review:T1" in f.ce.st.tickets and f.status("review:T1") != "passed":
        await f.review("T1")
    run = f.run("oracle.2", "oracle")                          # the oracle's own session
    assert run.sandbox == oracle.sandbox and run.resumed_session == "session-of-the-oracle"
    # its sandbox's token still names the ticket it was made for: bound to the sandbox, the fix's
    # requests (MCP, git, A2A) are its own run's
    b = f.ctl.auth.sandbox_binding(run.sandbox_ref)
    assert b is not None and b.task == "oracle"
    bearer = f.ctl.auth.identity.tokens.issue(b, "spiffe://t/" + b.sandbox)
    assert f.ctl.auth.live_run(bearer) == (f.ce.cid, run)
    assert f.ctl.auth.git_grant(bearer) is not None
    assert f.ctl.a2a_caller(bearer)[1].task == "oracle.2"
    await f.oracle(tid="oracle.2", files={TEST: "grep -qi hello src/greet.txt\n"})
    await f.signoff("H-2.2")                                   # the revised tests, signed
    await f.settle()
    assert f.status("verify:delivery") == "passed"            # delivery judged again, after it
    await f.integration()
    await f.settle()
    assert f.status("delivery") == "passed"


@scenario()
async def test_a_second_defect_is_told_to_the_fix_still_to_come(f: Flow):
    await f.to_build(R2)
    oracle = f.runs("oracle", "oracle")[-1]
    impl = f.run("T1", "implementer")
    async with client(f) as c:
        await rpc(c, "oracle@oracle", send("two questions"), token(f, impl))
        for _ in range(250):
            turns = [r for r in f.ce.runs.values() if r.consult and r.role == "oracle"
                     and r.id in f.ctl.svc.procs]
            if turns:
                break
            await f.settle()
        [turn] = turns
        assert await f.ce.test_defect(turn, [TEST], WHY) == {"fix": "oracle.2", "queued": False}
        out = await f.ce.test_defect(turn, [TEST], "and it ignores a trailing newline")
        assert out == {"fix": "oracle.2", "queued": True}         # waiting to be applied: told
        await consult_answers(f, "oracle", "oracle", "both confirmed", f"session-{oracle.id}")
    await f.implement("T1")
    await f.settle()
    fix = f.ticket("oracle.2")
    assert sum("BLOCKER" in x for x in fix.findings) == 2 and "oracle.3" not in f.ce.st.tickets
