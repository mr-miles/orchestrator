"""A run is the orchestrator's A2A task in the agent's one inbox (services/run_tasks.py):
``report`` records, ``a2a_reply`` ends the task, and the orchestrator asks again when the reply
does not match the record."""
from __future__ import annotations

import asyncio

import httpx
from engine_harness import Flow, scenario, signals
from test_a2a import V, client, rpc, send, task_when, to_critique, token

from legion_orch.identity import WorkloadBinding
from legion_orch.server import A2AHub
from legion_orch.workflow.model import ACCEPTED, ESCALATED, R_FAILED, R_REPORTED


async def intake_report(f: Flow, run, **kw):
    atts = [await f.write(run, "intake/requirements.md", "# Requirements\n\n- REQ-1: hello\n"),
            await f.write(run, "intake/risk.json", signals())]
    return await f.report(run, atts, exit_agent=False, **kw)


@scenario()
async def test_the_assignment_is_the_tasks_first_message_and_the_prompt_says_where(f: Flow):
    run = f.run("intake")
    prompt = f.runner.payloads[run.sandbox]["prompt.md"]
    assert "inbox()" in prompt and run.id in prompt and "a2a_reply" in prompt
    view = f.ce.task_view(run)
    assert view["task_id"] == run.id and view["from"] == "orchestrator"
    assert view["state"] == "working" and view["messages"][0]["text"] == run.instructions
    assert "intake" in run.instructions and len(run.instructions) > len(prompt)


@scenario()
async def test_a_reply_the_record_does_not_support_is_asked_again(f: Flow):
    run = f.run("intake")
    out = await f.reply(run, "completed")                      # nothing reported yet
    assert not out["ok"] and "no accepted report" in out["message"] and run.attempts == 1
    assert f.ce.task_view(run)["state"] == "working"
    await intake_report(f, run, status="DONE")
    out = await f.reply(run, "input-required")                 # the record says DONE
    assert not out["ok"] and 'state="completed"' in out["message"]
    out = await f.reply(run, "completed")
    assert out["ok"] and f.ce.task_view(run)["state"] == "completed"
    assert f.status("intake") != ACCEPTED                       # its turn has not ended yet
    await f.exit_agent(run)
    assert run.concluded and f.status("intake") == ACCEPTED


@scenario()
async def test_blocked_then_input_required_escalates(f: Flow):
    run = f.run("intake")
    await intake_report(f, run, status="BLOCKED", blocked_reason="the goal is ambiguous",
                        missing_decision="greet in which language?")
    assert (await f.reply(run, "input-required"))["ok"]
    await f.exit_agent(run)
    assert f.status("intake") == ESCALATED


@scenario()
async def test_failed_gives_up_and_the_run_fails(f: Flow):
    run = f.run("intake")
    out = await f.reply(run, "failed", "I cannot read the repository")
    assert out["ok"] and run.status == R_FAILED
    assert "gave up" in run.failure.reason and "cannot read" in run.failure.reason
    await f.settle()
    assert f.status("intake") == ESCALATED


@scenario()
async def test_the_agent_keeps_its_mcp_access_until_it_ends_its_task(f: Flow):
    run = f.run("intake")
    b = WorkloadBinding(sandbox=run.sandbox_ref, change=f.ce.cid, task=run.node_id, role=run.role)
    tok = f.ctl.auth.identity.tokens.issue(b, "spiffe://t/x")
    await intake_report(f, run, status="DONE")
    assert run.status == R_REPORTED and f.ctl.run_for_bearer(tok) is not None   # may reply
    await f.reply(run, "completed")
    assert f.ctl.run_for_bearer(tok) is None                     # its task is over


@scenario()
async def test_ending_the_task_with_messages_waiting_reminds_of_each_message_once(f: Flow):
    planner, critic = await to_critique(f)
    async with client(f) as c:
        await rpc(c, "critic@plan", send("Is REQ-1 final?"), token(f, planner))
        for _ in range(100):
            if A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@plan"):
                break
            await asyncio.sleep(0.02)
    doc = await f.write(critic, f"plan/critique/cycle-{critic.cycle}.md", "critique PASS")
    plan = [f"file:changes/{f.ce.cid}/plan/{p}@1" for p in ("spec.md", "oracle.json",
                                                            "taskgraph.json")]
    out = await f.ce.handle_report(critic.id, "DONE", "ok", plan + [doc],
                                   {"verdict": "PASS", "findings": [], "checked": ["x"]},
                                   None, None)
    assert out.accepted, out
    waiting = [(i.task_id, i.sender) for i in A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@plan")]
    attempts = critic.attempts
    first = await f.ce.reply_task(critic, "completed", "done", waiting)
    assert not first["ok"] and first["message"].startswith("1 task in your inbox")
    assert "`inbox`" in first["message"]
    assert critic.attempts == attempts                           # no report attempt used
    later = waiting + [("t-new", "implementer@build:T9")]           # one arrived since
    second = await f.ce.reply_task(critic, "completed", "done", later)
    assert not second["ok"] and second["message"].startswith("2 tasks in your inbox")
    third = await f.ce.reply_task(critic, "completed", "done", later)
    assert third["ok"]                              # nothing it was not told of: taken


@scenario()
async def test_a_peer_can_ask_back_or_decline(f: Flow):
    planner, critic = await to_critique(f)
    async with client(f) as c:
        for state, expect in (("input-required", "TASK_STATE_INPUT_REQUIRED"),
                              ("rejected", "TASK_STATE_REJECTED")):
            res = await rpc(c, "critic@plan", send(f"question ({state})"), token(f, planner))
            tid = res["task"]["id"]
            for _ in range(100):
                if A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@plan"):
                    break
                await asyncio.sleep(0.02)
            hub = A2AHub.of(f.ctl)
            assert (await hub.agent_reply(f.ce, critic, tid, "which REQ do you mean?", state))["ok"]
            t = await task_when(c, "critic@plan", tid, token(f, planner),
                                done=(expect,))
            assert t["status"]["state"] == expect
            assert "which REQ" in str(t["status"]["message"])
    assert httpx and V                                          # (helpers shared with test_a2a)


@scenario()
async def test_a_question_back_is_in_the_senders_inbox_and_its_reply_continues_the_task(f: Flow):
    """critic@plan answers the planner's message with a question (input-required): the planner
    sees it in its inbox, and its reply is the next message on the same task."""
    planner, critic = await to_critique(f)
    async with client(f) as c:
        res = await rpc(c, "critic@plan", send("Is REQ-1 final?"), token(f, planner))
        tid = res["task"]["id"]
        for _ in range(100):
            if A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@plan"):
                break
            await asyncio.sleep(0.02)
        assert (await A2AHub.of(f.ctl).agent_reply(f.ce, critic, tid, "Final as of which spec version?",
                                     "input-required"))["ok"]
        await task_when(c, "critic@plan", tid, token(f, planner),
                        done=("TASK_STATE_INPUT_REQUIRED",))
        [q] = A2AHub.of(f.ctl).inbox.questions_for(f.ce.cid, "planner@plan")
        assert (q.task_id, q.card) == (tid, "critic@plan") and q.view()["kind"] == "question"
        hub = A2AHub.of(f.ctl)
        out = await hub.agent_reply(f.ce, planner, tid, "Version 2.", "completed")   # its answer
        assert out["ok"] and out["state"].startswith("sent")
        for _ in range(100):
            if A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@plan"):
                break
            await asyncio.sleep(0.02)
        [item] = A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@plan")
        assert item.task_id == tid and item.text == "Version 2."     # the same conversation
        assert not A2AHub.of(f.ctl).inbox.questions_for(f.ce.cid, "planner@plan")
        assert (await A2AHub.of(f.ctl).agent_reply(f.ce, critic, tid, "Then yes, final.", "completed"))["ok"]
        t = await task_when(c, "critic@plan", tid, token(f, planner))
        assert t["status"]["state"] == "TASK_STATE_COMPLETED"
