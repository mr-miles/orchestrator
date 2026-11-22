"""Agent-to-agent (A2A): the change's orchestrator card and one card per step agent with a
sandbox, served on the A2A listener; the gateway-injected bearer scopes a caller to its change.

A message to a running agent waits in its inbox (MCP inbox / a2a_reply); to one that is not
running – its sandbox stopped between turns – the controller queues it, starts the sandbox,
resumes the agent's session for one turn, sends back its answer and stops the sandbox again."""
from __future__ import annotations

import asyncio
import json

import httpx
from engine_harness import TASKS, Flow, scenario
from test_sessions import give_session

from legion_orch.identity import WorkloadBinding
from legion_orch.server import MCP_TOOLS, A2AHub, build_a2a_app, build_app
from legion_orch.server.views import change_state
from legion_orch.workflow import attention
from legion_orch.workflow.cards import agent_state
from legion_orch.workflow.model import Run

V = {"A2A-Version": "1.0"}


def token(f: Flow, run: Run, *, role: str | None = None, change: str | None = None) -> dict:
    b = WorkloadBinding(sandbox=run.sandbox_ref, change=change or f.ce.cid, task=run.node_id,
                        role=role or run.role)
    return {"Authorization": "Bearer " + f.ctl.auth.identity.tokens.issue(b, "spiffe://t/" + b.sandbox)}


def client(f: Flow) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_a2a_app(f.ctl)),
                             base_url="http://a2a")


def send(text: str | None = None, data: dict | None = None, *, wait: bool = False) -> dict:
    parts = ([{"text": text}] if text is not None else []) + ([{"data": data}] if data else [])
    return {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {"messageId": f"m{id(parts)}", "role": "ROLE_USER",
                                   "parts": parts},
                       "configuration": {"returnImmediately": not wait}}}


async def rpc(c, card: str, body: dict, auth: dict) -> dict:
    r = await c.post(f"/a2a/{card}", json=body, headers=auth | V)
    assert r.status_code == 200, r.text
    out = r.json()
    assert "error" not in out, out
    return out["result"]


async def task_when(c, card, tid, auth, done=("TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
                                                    "TASK_STATE_REJECTED")) -> dict:
    for _ in range(250):
        t = await rpc(c, card, {"jsonrpc": "2.0", "id": 2, "method": "GetTask",
                                     "params": {"id": tid}}, auth)
        if t["status"]["state"] in done:
            return t
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {tid} stayed {t['status']}")


def answer_text(t: dict) -> str:
    return "\n".join(p.get("text", "") for a in t.get("artifacts", []) for p in a["parts"])


async def to_critique(f: Flow) -> tuple[Run, Run]:
    """The planner reported (its sandbox kept, stopped, with its session); the critic is live."""
    await f.intake()
    await f.human("H-1", "accept")
    planner = f.run("plan")
    give_session(f, planner)
    await f.plan()
    return planner, f.run("plan", "critic")


@scenario()
async def test_cards_follow_the_sandboxes_and_are_scoped_to_the_callers_change(f: Flow):
    intake = f.run("intake")
    async with client(f) as c:
        reg = (await c.get("/a2a/cards", headers=token(f, intake))).json()
        assert reg["you"] == "intake@intake"
        assert [x["id"] for x in reg["cards"]] == ["orchestrator", "resolver"]
        planner, critic = await to_critique(f)
        assert intake.sandbox in f.runner.deleted                 # H-1 accepted: intake gone
        ids = [x["id"] for x in (await c.get("/a2a/cards", headers=token(f, critic))).json()["cards"]]
        assert ids == ["orchestrator", "resolver", "planner@plan"]    # not its own card
        cards = {x.id: x for x in f.ce.a2a_cards()}
        assert set(cards) == {"orchestrator", "resolver", "planner@plan", "critic@plan"}
        assert agent_state(f.ce, cards["planner@plan"]) == "stopped"     # still published
        assert agent_state(f.ce, cards["critic@plan"]) == "running"
        assert "stopped" not in cards["planner@plan"].model_dump()        # no state in a card
        events = [e for e in f.ce.timeline().splitlines() if '"a2a"' in e]
        assert any("card registered: intake@intake" in e for e in events)
        assert any("cards removed: intake" in e for e in events)

        # the card, at its own URL (as the a2a CLI fetches a URL with a path) and well-known
        card = (await c.get("/a2a/planner@plan", headers=token(f, critic))).json()
        assert card["name"] == f"{f.ce.cid}/planner@plan"
        assert card["supportedInterfaces"][0]["url"].endswith("/a2a/planner@plan")
        assert (await c.get("/a2a/orchestrator/.well-known/agent-card.json",
                            headers=token(f, critic))).json()["skills"][0]["id"] == "list_cards"

        # scope: the bearer's change only, live sandboxes only, never a verifier
        assert (await c.get("/a2a/cards")).status_code == 401
        assert (await c.get("/a2a/cards", headers=token(f, intake))).status_code == 401
        assert (await c.get("/a2a/cards", headers=token(f, critic, role="verifier"))).status_code == 401
        assert (await c.get("/a2a/cards", headers=token(f, critic, change="c-other"))).status_code == 401
        assert (await c.get("/a2a/intake@intake", headers=token(f, critic))).status_code == 404

        # the same URLs for every change: a second change's agent sees only its own cards
        other = await f.ctl.start_change("Say goodbye", "app")
        await f.settle()
        theirs = next(r for r in other.runs.values() if r.role == "intake")
        mine = (await c.get("/a2a/cards", headers=token(f, theirs, change=other.cid))).json()
        assert mine["change"] == other.cid
        assert [x["id"] for x in mine["cards"]] == ["orchestrator", "resolver"]
        assert (await c.get("/a2a/planner@plan",
                            headers=token(f, theirs, change=other.cid))).status_code == 404
        own = await c.get("/a2a/orchestrator", headers=token(f, theirs, change=other.cid))
        assert own.json()["name"] == f"{other.cid}/orchestrator"      # same URL, its change

        # the same as an AI Catalog: what run-agent.sh registers with `a2a catalog add`
        cat = (await c.get("/a2a/ai-catalog.json", headers=token(f, critic))).json()
        assert cat["specVersion"] == "1.0"
        assert [(e["identifier"], e["type"]) for e in cat["entries"]] == [
            ("urn:air:legion:agent:orchestrator", "application/a2a-agent-card+json"),
            ("urn:air:legion:agent:resolver", "application/a2a-agent-card+json"),
            ("urn:air:legion:agent:planner@plan", "application/a2a-agent-card+json")]
        assert cat["entries"][2]["url"].endswith("/a2a/planner@plan")
        assert cat["entries"][2]["tags"] == ["planner", "plan"]           # who, not how it is
        env = next(e for e in f.runner.run_envs if "LEGION_ROLE=critic" in e)
        assert "LEGION_A2A_CATALOG=http://host.openshell.internal:8769/a2a/ai-catalog.json" \
            in env.replace("'", "")
        assert "A2ACLI_AGENT_CARD=" in env and "LEGION_A2A_CARD=critic@plan" in env.replace("'", "")

        # the orchestrator's registry, through A2A
        res = await rpc(c, "orchestrator", send("/list_cards", wait=True), token(f, critic))
        listed = res["task"]["artifacts"][0]["parts"][0]["data"]["cards"]
        assert [x["id"] for x in listed] == ["orchestrator", "resolver", "planner@plan"]
        assert all("state" not in x for x in listed)


@scenario()
async def test_a_message_to_a_stopped_agent_starts_it_resumes_its_session_and_stops_it_again(f: Flow):
    planner, critic = await to_critique(f)
    sb = planner.sandbox
    assert f.ce.nodes["plan"].sandbox_stopped and sb in f.runner.stopped
    f.runner.reflection_text = "The oracle checks greet.txt only."
    stops = f.runner.stopped.count(sb)
    async with client(f) as c:
        res = await rpc(c, "planner@plan", send("What does the oracle check?"),
                        token(f, critic))
        t = await task_when(c, "planner@plan", res["task"]["id"], token(f, critic))
    assert t["status"]["state"] == "TASK_STATE_COMPLETED", t["status"]["message"]["parts"]
    assert answer_text(t) == "The oracle checks greet.txt only."
    assert sb in f.runner.restarted                                  # started for the turn
    name, prompt = f.runner.reflected[-1]
    assert name == sb and "`critic@plan`" in prompt and "What does the oracle check?" in prompt
    assert f"session-{planner.id}" in f.runner.run_envs[-1]          # its own session resumed
    assert f.runner.stopped.count(sb) == stops + 1                   # and stopped again
    assert f.ce.nodes["plan"].sandbox_stopped
    assert any(f.ce.latest(f"control/logs/{planner.id}-a2a{n}.log") for n in (1, 2))
    # the rework still finds it stopped, starts it, and resumes the same session
    await f.critique("REWORK")
    assert f.run("plan").sandbox == sb


@scenario()
async def test_a_message_to_a_running_agent_waits_in_its_inbox(f: Flow):
    planner, critic = await to_critique(f)
    assert {"inbox", "a2a_reply"} <= set(MCP_TOOLS)
    async with client(f) as c:
        res = await rpc(c, "critic@plan", send("Is the spec's REQ-1 section final?"),
                        token(f, planner))                          # the idle planner asks
        tid = res["task"]["id"]
        for _ in range(100):
            if A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@plan"):
                break
            await asyncio.sleep(0.02)
        [item] = A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@plan")
        assert item.view()["from"] == "planner@plan" and "REQ-1" in item.text
        assert (await A2AHub.of(f.ctl).agent_reply(f.ce, critic, tid, "Yes, final.", "completed"))["ok"]
        t = await task_when(c, "critic@plan", tid, token(f, planner))
        assert answer_text(t) == "Yes, final." and not A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@plan")
        # the sender's tasks are its own
        r = await c.post("/a2a/critic@plan", headers=token(f, critic) | V,
                         json={"jsonrpc": "2.0", "id": 3, "method": "GetTask", "params": {"id": tid}})
        assert "error" in r.json()


@scenario()
async def test_an_unanswered_message_is_answered_in_a_turn_once_the_run_ends(f: Flow):
    """T2's implementer asks T1's while it works; T1 reports without answering: its sandbox is
    kept (stopped) for rework, so one resumed turn of its session answers."""
    t2 = dict(TASKS[0], id="T2", title="part", files_modified=["src/part.txt"],
              verification=["test -f src/part.txt"])
    await f.to_build(tasks=[TASKS[0], t2])
    t1, other = f.run("build:T1", "implementer"), f.run("build:T2", "implementer")
    give_session(f, t1)
    f.runner.reflection_text = "greet.txt holds the greeting."
    async with client(f) as c:
        res = await rpc(c, "implementer@build:T1", send("Where does the greeting live?"),
                        token(f, other))
        await asyncio.sleep(0.2)
        assert A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "implementer@build:T1")   # waits: T1 is live
        await f.implement("T1")                                  # reports without answering
        t = await task_when(c, "implementer@build:T1", res["task"]["id"], token(f, other))
    assert t["status"]["state"] == "TASK_STATE_COMPLETED", t["status"]["message"]["parts"]
    assert answer_text(t) == "greet.txt holds the greeting."
    assert any("Where does the greeting live?" in p for _, p in f.runner.reflected)


@scenario()
async def test_a_judge_that_reported_is_kept_stopped_and_answers_from_its_session(f: Flow):
    """A judge's sandbox is stopped after its report, not deleted, until its step ends: a
    message resumes its session."""
    planner, critic = await to_critique(f)
    give_session(f, critic)
    f.runner.reflection_text = "No blockers: the spec covers REQ-1."
    async with client(f) as c:
        res = await rpc(c, "critic@plan", send("Any blockers?"), token(f, planner))
        await asyncio.sleep(0.2)
        await f.critique("REWORK")           # it reports without answering; the plan reworks
        assert critic.sandbox not in f.runner.deleted and critic.sandbox in f.runner.stopped
        t = await task_when(c, "critic@plan", res["task"]["id"], token(f, planner))
    assert t["status"]["state"] == "TASK_STATE_COMPLETED", t["status"]
    assert answer_text(t) == "No blockers: the spec covers REQ-1."
    await f.plan()                            # the next cycle's critic gets a fresh sandbox
    second = f.run("plan", "critic")
    assert second.sandbox != critic.sandbox and critic.sandbox in f.runner.deleted


async def consult_answers(f: Flow, role: str, node: str, answer: str, session: str) -> Run:
    """Play the agent started early: wait for its consult run, answer, exit."""
    for _ in range(250):
        found = [r for r in f.ce.runs.values() if r.consult and r.role == role
                 and r.node_id == node and r.id in f.ctl.svc.procs]
        if found:
            break
        await asyncio.sleep(0.02)
    [run] = found
    with open(f.ce.run_dir(run.id) / "agent.log", "a") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init", "session_id": session}) + "\n")
        fh.write(json.dumps({"type": "result", "subtype": "success", "result": answer}) + "\n")
    f.ctl.svc.procs[run.id].exit(0)
    return run


@scenario()
async def test_every_agent_of_a_step_has_a_card_from_the_steps_start(f: Flow):
    await f.to_build()
    panel = f.ce.change.panel
    assert panel                                            # the tier's judges
    cards = {c.id: c for c in f.ce.a2a_cards()}
    assert {f"{r}@build:T1" for r in ["implementer", *panel]} <= set(cards)
    assert agent_state(f.ce, cards[f"{panel[0]}@build:T1"]) == "not started"
    assert agent_state(f.ce, cards["implementer@build:T1"]) == "running"
    assert not [c for c in cards if c.endswith("@plan")]    # an ended step has none
    await f.implement()
    judges = [f.run("build:T1", r) for r in panel]
    for r in panel[:-1]:
        await f.review(role=r)
    first = judges[0]
    if len(panel) > 1:                      # a judge that reported is kept, stopped
        assert first.sandbox in f.runner.stopped and first.sandbox not in f.runner.deleted
    assert "implementer@build:T1" in {c.id for c in f.ce.a2a_cards()}   # still in progress
    await f.review(role=panel[-1])
    assert f.status("build:T1") == "ACCEPTED"
    assert not [c for c in f.ce.a2a_cards() if c.node == "build:T1"]    # the step ended
    await f.settle()
    assert all(j.sandbox in f.runner.deleted for j in judges)          # and its judges went


@scenario()
async def test_an_agent_not_started_is_started_to_answer_and_its_run_continues_it(f: Flow):
    await f.to_build()
    judge = f.ce.change.panel[0]
    impl = f.run("build:T1", "implementer")
    async with client(f) as c:
        res = await rpc(c, f"{judge}@build:T1", send("What will you check first?"),
                        token(f, impl))
        early = await consult_answers(f, judge, "build:T1", "Error handling first.", "sess-early")
        t = await task_when(c, f"{judge}@build:T1", res["task"]["id"], token(f, impl))
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and answer_text(t) == "Error handling first."
    assert early.id not in f.ce.nodes["build:T1"].runs        # not one of the step's runs
    payload = f.runner.payloads[early.sandbox]
    assert "What will you check first?" in payload["prompt.md"] and "change nothing" in payload["prompt.md"]
    await f.settle()
    assert early.sandbox in f.runner.stopped and early.sandbox not in f.runner.deleted
    assert agent_state(f.ce, {c.id: c for c in f.ce.a2a_cards()}[f"{judge}@build:T1"]) == "stopped"
    # a consult cannot report
    out = await f.ce.handle_report(early.id, "DONE", "x", [], {}, None, None)
    assert not out.accepted
    # the step gets to the review: the judge's run continues the agent started early
    await f.implement()
    run = f.run("build:T1", judge)
    assert run.sandbox == early.sandbox and run.resumed_session == "sess-early"
    assert early.sandbox in f.runner.restarted


@scenario()
async def test_unknown_skills_are_refused_and_relay_is_gone(f: Flow):
    planner, critic = await to_critique(f)
    async with client(f) as c:
        for body in (send("/relay planner@plan hi", wait=True), send("hello", wait=True)):
            res = await rpc(c, "orchestrator", body, token(f, critic))
            assert res["task"]["status"]["state"] == "TASK_STATE_REJECTED", res


@scenario()
async def test_ask_human_is_an_attention_item_until_a_decision_owner_answers(f: Flow):
    planner, critic = await to_critique(f)
    async with client(f) as c:
        res = await rpc(c, "orchestrator",
                        send("/ask_human May the greeting be in French?"), token(f, critic))
        tid = res["task"]["id"]
        for _ in range(100):
            if tid in f.ce.change.questions:
                break
            await asyncio.sleep(0.02)
        q = f.ce.change.questions[tid]
        assert (q.node, q.role, q.question) == ("plan", "critic", "May the greeting be in French?")
        [item] = [i for i in attention.items(f.ce) if i.kind == attention.QUESTION]
        assert item.blocking and item.detail == tid and "French" in item.reason
        assert change_state(f.ctl, f.ce)["a2a"]["questions"][0]["id"] == tid

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(f.ctl)),
                                     base_url="http://op") as op:
            url = f"/api/changes/{f.ce.cid}/questions/{tid}/answer"
            r = await op.post(url, json={"answer": "Oui", "approver": "mallory"})
            assert r.status_code == 400 and "decision owner" in r.json()["error"]
            r = await op.post(url, json={"answer": "Yes, in French.", "approver": "miles"})
            assert r.status_code == 200 and r.json()["answered_by"] == "miles"
            assert (await op.post(url, json={"answer": "again", "approver": "miles"})).status_code == 400
        t = await task_when(c, "orchestrator", tid, token(f, critic))
    assert answer_text(t) == "Yes, in French."
    assert not [i for i in attention.items(f.ce) if i.kind == attention.QUESTION]


@scenario()
async def test_open_questions_are_withdrawn_when_the_controller_restarts(f: Flow):
    planner, critic = await to_critique(f)
    async with f.ce.lock:
        f.ce.ask_human("t-1", "plan", "critic", critic.id, "Which locale?")
    f.ce.recover()
    q = f.ce.change.questions["t-1"]
    assert not q.open and "restarted" in q.withdrawn
    assert not [i for i in attention.items(f.ce) if i.kind == attention.QUESTION]
    f.ce.load()                                            # it was saved withdrawn
    assert f.ce.change.questions["t-1"].withdrawn


def test_run_agent_registers_the_changes_catalog_with_the_a2a_cli(tmp_path):
    """At start-up the agent's a2a CLI learns the change's catalog: added in a new sandbox,
    updated in a reused one. Without the CLI the agent still starts."""
    import shlex
    import subprocess

    from legion_orch.sandbox import RUN_AGENT_SH
    p, bin_ = tmp_path / "sb" / "payload", tmp_path / "bin"
    p.mkdir(parents=True)
    bin_.mkdir()
    (p / "run-agent.sh").write_text(RUN_AGENT_SH)
    calls = tmp_path / "a2a-calls"
    # a fake a2a: `catalog update` fails until `catalog add` has registered it
    (bin_ / "a2a").write_text(f"""#!/bin/bash
echo "$* auth=${{A2ACLI_AUTH:-}}" >> {calls}
if [ "$2" = update ]; then [ -f {tmp_path}/added ]; exit $?; fi
touch {tmp_path}/added
""")
    (bin_ / "a2a").chmod(0o755)
    env = {"LEGION_NEEDS_REPO": "0", "LEGION_AGENT_COMMAND": "echo started > ../started",
           "LEGION_A2A_CATALOG": "http://h:8769/a2a/ai-catalog.json"}
    (p / "run.env").write_text("".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()))

    def start(path: str, **extra) -> str:
        out = subprocess.run(["bash", str(p / "run-agent.sh")], capture_output=True, text=True,
                             env={"PATH": path, "HOME": str(tmp_path)} | extra)
        assert out.returncode == 0, out.stderr
        assert (tmp_path / "sb" / "started").read_text().strip() == "started"
        return out.stdout

    assert "registered A2A catalog legion" in start(f"{bin_}:/usr/bin:/bin")
    assert "registered A2A catalog legion" in start(f"{bin_}:/usr/bin:/bin",
                                                    LEGION_ACCESS_TOKEN="tok")
    assert calls.read_text().splitlines() == [
        "catalog update legion auth=",
        "catalog add legion http://h:8769/a2a/ai-catalog.json auth=",
        "catalog update legion auth=Bearer tok"]                 # reused: updated (dev auth)
    assert "no a2a CLI on PATH" in start("/usr/bin:/bin")


def drift_check(f: Flow):
    from legion_orch.server.a2a import A2AHub
    hub = A2AHub.of(f.ctl)

    def check(where: str) -> None:
        pushed = sorted(c.id for c in hub.catalog.cards(f.ce.cid))
        implied = sorted(c.id for c in f.ce.a2a_cards())
        assert pushed == implied, (where, pushed, implied)
    return hub, check


@scenario()
async def test_the_catalog_never_drifts_from_the_workflow_through_planning(f: Flow):
    """The cards the workflow pushed (publish as a step starts, withdraw as it ends) are, after
    every step, exactly the cards its state implies; a catalog subscribing later gets the same."""
    from legion_orch.server.a2a import Catalog
    hub, check = drift_check(f)
    check("start")
    await f.intake()
    check("intake reported")
    await f.human("H-1", "accept")
    check("H-1")
    await f.plan()
    check("plan")
    await f.critique("REWORK")
    check("critique rework")
    await f.plan()
    await f.critique("PASS")
    check("critique pass")
    late = Catalog()
    f.ctl.subscribe_cards(late)                       # a catalog subscribing later (a restart)
    assert sorted(c.id for c in late.cards(f.ce.cid)) == sorted(c.id for c in f.ce.a2a_cards())


@scenario()
async def test_the_catalog_never_drifts_from_the_workflow_through_build_and_the_end(f: Flow):
    hub, check = drift_check(f)
    await f.to_build()
    check("build")
    await f.implement()
    check("implemented")
    await f.review(verdict="FAIL")
    check("review fail: rework")
    run = f.run("build:T1", "implementer")                 # cycle 2: it blocks, it escalates
    out = await f.report(run, [], status="BLOCKED", blocked_reason="the greeting is ambiguous",
                         missing_decision="which language?")
    assert out.accepted, out
    check("escalated")
    await f.human("build:T1", "abandon", reason="the goal changed: no greeting needed at all")
    check("abandoned")
    assert hub.catalog.cards(f.ce.cid) == []
