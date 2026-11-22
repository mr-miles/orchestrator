"""The resolver (WORKFLOW.md §5.16): every escalation is routed to the change's resolver agent,
which diagnoses it and proposes one of the step's human actions; a human applies, edits or
ignores the proposal. Agents may ask the resolver directly."""
from __future__ import annotations

import asyncio
import json

from engine_harness import Flow, scenario
from test_a2a import client, rpc, send, task_when, token

from legion_orch.server import A2AHub
from legion_orch.workflow import attention
from legion_orch.workflow.cards import agent_state
from legion_orch.workflow.model import ESCALATED


async def blocked(f: Flow):
    """The intake reports BLOCKED and ends its task: the step escalates."""
    run = f.run("intake")
    out = await f.report(run, [], status="BLOCKED", blocked_reason="the goal is ambiguous",
                         missing_decision="greet in which language?")
    assert out.accepted, out
    assert f.status("intake") == ESCALATED
    return run


async def play_resolver(f: Flow, answer: str, session: str = "sess-resolver"):
    """Wait for the resolver's turn, answer, end it; returns its run."""
    for _ in range(250):
        live = [r for r in f.ce.runs.values() if r.role == "resolver" and r.id in f.ctl.svc.procs
                and f.ctl.svc.procs[r.id].returncode is None]
        if live:
            break
        await asyncio.sleep(0.02)
    [run] = live
    with open(f.ce.run_dir(run.id) / "agent.log", "a") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init", "session_id": session}) + "\n")
        fh.write(json.dumps({"type": "result", "subtype": "success", "result": answer}) + "\n")
    f.ctl.svc.procs[run.id].exit(0)
    return run


def proposal(action: str, **payload) -> str:
    return ("The intake could not tell the language; the goal says 'Say hello'.\n\n```json\n"
            + json.dumps({"proposal": {"action": action, "payload": payload,
                                       "rationale": "English is the repository's language",
                                       "attestations": []}}) + "\n```")


@scenario()
async def test_the_resolver_has_a_card_for_the_whole_change(f: Flow):
    card = {c.id: c for c in f.ce.a2a_cards()}["resolver"]
    assert agent_state(f.ce, card) == "not started" and card.role == "resolver"


@scenario()
async def test_an_escalation_is_routed_to_the_resolver_and_its_proposal_is_shown(f: Flow):
    A2AHub.of(f.ctl)                                     # the A2A listener is up
    await blocked(f)
    actions = f.ce.workflow.actions("intake")
    assert "answer" in actions
    res = await play_resolver(f, proposal("answer", answer="Greet in English."))
    assert res.consult and res.node_id == "resolver"
    prompt = f.runner.payloads[res.sandbox]["prompt.md"]
    assert "Step `intake` escalated" in prompt and "greet in which language?" in prompt
    await f.settle()
    esc = f.ce.nodes["intake"].escalation
    assert esc.proposal and esc.proposal.action == "answer"
    assert esc.proposal.payload == {"answer": "Greet in English."}
    assert f.ce.latest("control/proposals/intake-1.json")
    [item] = [i for i in attention.items(f.ce) if i.kind == attention.ESCALATION]
    assert item.proposal.startswith("answer: English")
    assert res.sandbox in f.runner.stopped and res.sandbox not in f.runner.deleted   # kept
    # a human applies it as an ordinary decision
    await f.human("intake", "answer", answer=esc.proposal.payload["answer"])
    assert f.status("intake") != ESCALATED


@scenario()
async def test_an_invalid_proposal_is_returned_with_the_reason(f: Flow):
    A2AHub.of(f.ctl)
    await blocked(f)
    first = await play_resolver(f, proposal("merge_everything"))
    second = await play_resolver(f, proposal("answer", answer="English."))
    assert second.sandbox == first.sandbox                     # the same resolver, resumed
    assert second.resumed_session == "sess-resolver"
    assert "was not taken" in f.ce.timeline() or second.id != first.id     # asked again
    await f.settle()
    assert f.ce.nodes["intake"].escalation.proposal.action == "answer"


@scenario()
async def test_a_proposal_after_the_human_decided_is_stale(f: Flow):
    A2AHub.of(f.ctl)
    await blocked(f)
    await f.human("intake", "answer", answer="English, decided already")
    await play_resolver(f, proposal("answer", answer="English."))
    await f.settle()
    node = f.ce.nodes["intake"]
    assert node.escalation is None or node.escalation.proposal is None
    assert "is stale" in f.ce.timeline()


@scenario()
async def test_an_agent_asks_the_resolver_directly(f: Flow):
    intake = f.run("intake")
    async with client(f) as c:
        res = await rpc(c, "resolver", send("May I read files outside the repo?"), token(f, intake))
        await play_resolver(f, "No: work only from the checkout and the documents.")
        t = await task_when(c, "resolver", res["task"]["id"], token(f, intake))
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "only from the checkout" in str(t["artifacts"])
