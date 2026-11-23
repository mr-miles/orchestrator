"""Agent-to-agent (A2A): the change's orchestrator card and one card per step agent with a
sandbox, served on the A2A listener; the gateway-injected bearer scopes a caller to its change.

A message to a running agent waits in its inbox (MCP inbox / a2a_reply); to one that is not
running the controller gives it a turn of its own – a consult run: started if it has not, else
resumed in place in its sandbox (only the Skill tool and MCP, under the sandbox's lock) – which
answers with a2a_reply or its final message; the sandbox is stopped again afterwards."""
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
    b = WorkloadBinding(sandbox=run.sandbox_ref, change=change or f.ce.cid, task=run.ticket,
                        role=role or run.role)
    return {"Authorization": "Bearer " + f.ctl.auth.identity.tokens.issue(b, "spiffe://t/" + b.sandbox)}


def catalog(f: Flow) -> list:
    """The change's cards, as the A2A catalog keeps them."""
    return A2AHub.of(f.ctl).cards(f.ce)


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
    await f.signoff("H-1")
    planner = f.run("design")
    give_session(f, planner)
    await f.plan()
    return planner, f.run("critique", "critic")


async def listed(c, tok) -> list[str]:
    """The cards the caller may message: its change's AI Catalog (the one way to find them)."""
    return [e["displayName"] for e in (await c.get("/a2a/ai-catalog.json", headers=tok)).json()["entries"]]


@scenario()
async def test_cards_follow_the_sandboxes_and_are_scoped_to_the_callers_change(f: Flow):
    intake = f.run("intake")
    async with client(f) as c:
        assert await listed(c, token(f, intake)) == ["orchestrator", "resolver"]
        planner, critic = await to_critique(f)
        assert intake.sandbox in f.runner.deleted                 # H-1 accepted: intake gone
        assert await listed(c, token(f, critic)) == ["orchestrator", "resolver", "planner@design"]
        cards = {x.id: x for x in catalog(f)}                     # (not its own card, above)
        assert set(cards) == {"orchestrator", "resolver", "planner@design", "critic@critique"}
        assert agent_state(f.ce, cards["planner@design"]) == "stopped"     # still published
        assert agent_state(f.ce, cards["critic@critique"]) == "running"
        assert "stopped" not in cards["planner@design"].model_dump()        # no state in a card

        # the card, at its own URL (as the a2a CLI fetches a URL with a path) and well-known
        card = (await c.get("/a2a/planner@design", headers=token(f, critic))).json()
        assert card["name"] == f"{f.ce.cid}/planner@design"
        assert card["supportedInterfaces"][0]["url"].endswith("/a2a/planner@design")
        skills = (await c.get("/a2a/orchestrator/.well-known/agent-card.json",
                              headers=token(f, critic))).json()["skills"]
        assert [x["id"] for x in skills] == ["ask_human"]       # discovery is the catalog's

        # scope: the bearer's change only, live sandboxes only, never a verifier
        cat = "/a2a/ai-catalog.json"
        assert (await c.get(cat)).status_code == 401
        assert (await c.get(cat, headers=token(f, intake))).status_code == 401
        assert (await c.get(cat, headers=token(f, critic, role="verifier"))).status_code == 401
        assert (await c.get(cat, headers=token(f, critic, change="c-other"))).status_code == 401
        assert (await c.get("/a2a/intake@intake", headers=token(f, critic))).status_code == 404

        # the same URLs for every change: a second change's agent sees only its own cards
        other = await f.ctl.start_change("Say goodbye", "app")
        await f.settle()
        theirs = next(r for r in other.runs.values() if r.role == "intake")
        mine = (await c.get(cat, headers=token(f, theirs, change=other.cid))).json()
        assert other.cid in mine["host"]["displayName"]
        assert [e["displayName"] for e in mine["entries"]] == ["orchestrator", "resolver"]
        assert (await c.get("/a2a/planner@design",
                            headers=token(f, theirs, change=other.cid))).status_code == 404
        own = await c.get("/a2a/orchestrator", headers=token(f, theirs, change=other.cid))
        assert own.json()["name"] == f"{other.cid}/orchestrator"      # same URL, its change

        # what run-agent.sh registers with `a2a catalog add`
        doc = (await c.get(cat, headers=token(f, critic))).json()
        assert doc["specVersion"] == "1.0"
        assert [(e["identifier"], e["type"]) for e in doc["entries"]] == [
            ("urn:air:legion:agent:orchestrator", "application/a2a-agent-card+json"),
            ("urn:air:legion:agent:resolver", "application/a2a-agent-card+json"),
            ("urn:air:legion:agent:planner@design", "application/a2a-agent-card+json")]
        assert doc["entries"][2]["url"].endswith("/a2a/planner@design")
        assert doc["entries"][2]["tags"] == ["planner", "design"]           # who, not how it is
        env = next(e for e in f.runner.run_envs if "LEGION_ROLE=critic" in e)
        assert "LEGION_A2A_CATALOG=http://host.openshell.internal:8769/a2a/ai-catalog.json" \
            in env.replace("'", "")
        assert "A2ACLI_AGENT_CARD=" in env and "LEGION_A2A_CARD=critic@critique" in env.replace("'", "")

        # the orchestrator explains what it does when asked something else
        res = await rpc(c, "orchestrator", send("/list_cards", wait=True), token(f, critic))
        assert res["task"]["status"]["state"] in ("TASK_STATE_REJECTED", "rejected"), res


@scenario()
async def test_a_message_to_a_stopped_agent_starts_it_resumes_its_session_and_stops_it_again(f: Flow):
    planner, critic = await to_critique(f)
    sb = planner.sandbox
    assert f.ce.runs[planner.id].sandbox_stopped and sb in f.runner.stopped
    stops = f.runner.stopped.count(sb)
    async with client(f) as c:
        res = await rpc(c, "planner@design", send("What does the oracle check?"),
                        token(f, critic))
        turn = await consult_answers(f, "planner", "design", "The oracle checks greet.txt only.",
                                     f"session-{planner.id}")
        t = await task_when(c, "planner@design", res["task"]["id"], token(f, critic))
    assert t["status"]["state"] == "TASK_STATE_COMPLETED", t["status"]["message"]["parts"]
    assert answer_text(t) == "The oracle checks greet.txt only."
    assert turn.sandbox == sb and turn.id not in f.ticket("design").runs   # in place, a consult
    assert sb in f.runner.restarted                                  # started for the turn
    prompt = f.runner.uploaded[sb]["prompt.md"]
    assert "`critic@critique`" in prompt and "What does the oracle check?" in prompt
    assert res["task"]["id"] in prompt                                # a2a_reply(task_id=...)
    env = next(e for e in reversed(f.runner.run_envs) if f"LEGION_RUN_ID={turn.id}" in e)
    assert f"session-{planner.id}" in env and "--tools Skill" in env  # its session; no edits
    await f.settle()
    assert f.runner.stopped.count(sb) == stops + 1                   # and stopped again
    assert f.ce.runs[planner.id].sandbox_stopped
    # the rework still finds it stopped, starts it, and resumes the same session
    await f.critique("REWORK")
    assert f.run("design").sandbox == sb


@scenario()
async def test_a_message_to_a_running_agent_waits_in_its_inbox(f: Flow):
    planner, critic = await to_critique(f)
    assert {"inbox", "a2a_reply"} <= set(MCP_TOOLS)
    async with client(f) as c:
        res = await rpc(c, "critic@critique", send("Is the spec's REQ-1 section final?"),
                        token(f, planner))                          # the idle planner asks
        tid = res["task"]["id"]
        for _ in range(100):
            if A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@critique"):
                break
            await asyncio.sleep(0.02)
        [item] = A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@critique")
        assert item.view()["from"] == "planner@design" and "REQ-1" in item.text
        assert (await A2AHub.of(f.ctl).agent_reply(f.ce, critic, tid, "Yes, final.", "completed"))["ok"]
        t = await task_when(c, "critic@critique", tid, token(f, planner))
        assert answer_text(t) == "Yes, final."
        assert not A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "critic@critique")
        # the sender's tasks are its own
        r = await c.post("/a2a/critic@critique", headers=token(f, critic) | V,
                         json={"jsonrpc": "2.0", "id": 3, "method": "GetTask", "params": {"id": tid}})
        assert "error" in r.json()


@scenario()
async def test_an_unanswered_message_is_answered_in_a_turn_once_the_run_ends(f: Flow):
    """T2's implementer asks T1's while it works; T1 reports without answering: its sandbox is
    kept (stopped) for rework, so a turn of its own, in place, answers."""
    t2 = dict(TASKS[0], id="T2", title="part", files_modified=["src/part.txt"],
              verification=["test -f src/part.txt"])
    await f.to_build(tasks=[TASKS[0], t2])
    t1, other = f.run("T1", "implementer"), f.run("T2", "implementer")
    give_session(f, t1)
    async with client(f) as c:
        res = await rpc(c, "implementer@T1", send("Where does the greeting live?"),
                        token(f, other))
        await asyncio.sleep(0.2)
        assert A2AHub.of(f.ctl).inbox.pending(f.ce.cid, "implementer@T1")   # waits: T1 is live
        await f.implement("T1")                                  # reports without answering
        turn = await consult_answers(f, "implementer", "T1",
                                     "greet.txt holds the greeting.", f"session-{t1.id}")
        t = await task_when(c, "implementer@T1", res["task"]["id"], token(f, other))
    assert t["status"]["state"] == "TASK_STATE_COMPLETED", t["status"]["message"]["parts"]
    assert answer_text(t) == "greet.txt holds the greeting."
    assert turn.sandbox == t1.sandbox
    assert "Where does the greeting live?" in f.runner.uploaded[t1.sandbox]["prompt.md"]


@scenario()
async def test_a_judge_that_reported_is_kept_stopped_and_answers_from_its_session(f: Flow):
    """A judge's sandbox is stopped after its report, not deleted, until its step ends: a
    message resumes its session."""
    planner, critic = await to_critique(f)
    give_session(f, critic)
    async with client(f) as c:
        res = await rpc(c, "critic@critique", send("Any blockers?"), token(f, planner))
        await asyncio.sleep(0.2)
        await f.critique("REWORK")           # it reports without answering; the plan reworks
        turn = await consult_answers(f, "critic", "critique", "No blockers: the spec covers REQ-1.",
                                     f"session-{critic.id}")
        assert turn.sandbox == critic.sandbox                     # in place
        t = await task_when(c, "critic@critique", res["task"]["id"], token(f, planner))
    assert t["status"]["state"] == "TASK_STATE_COMPLETED", t["status"]
    assert answer_text(t) == "No blockers: the spec covers REQ-1."
    await f.settle()
    assert critic.sandbox not in f.runner.deleted and critic.sandbox in f.runner.stopped
    await f.plan()                            # the next cycle's critic gets a fresh sandbox
    second = f.run("critique", "critic")
    assert second.sandbox != critic.sandbox and critic.sandbox in f.runner.deleted


async def consult_answers(f: Flow, role: str, node: str, answer: str, session: str) -> Run:
    """Play the agent started early: wait for its consult run, answer, exit."""
    for _ in range(250):
        found = [r for r in f.ce.runs.values() if r.consult and r.role == role
                 and r.ticket == node and r.id in f.ctl.svc.procs]
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
async def test_an_agents_card_appears_as_it_starts_and_goes_with_its_sandboxes(f: Flow):
    await f.to_build()
    cards = {c.id: c for c in catalog(f)}
    assert "implementer@T1" in cards and "reviewer@review:T1" not in cards   # not started
    assert agent_state(f.ce, cards["implementer@T1"]) == "running"
    assert "planner@design" not in cards                     # a closed ticket's agent is gone
    await f.implement()
    judge = f.run("review:T1", "reviewer")
    cards = {c.id: c for c in catalog(f)}
    assert {"implementer@T1", "reviewer@review:T1"} <= set(cards)   # both: stopped and running
    assert agent_state(f.ce, cards["implementer@T1"]) == "stopped"
    await f.review()
    assert f.status("T1") == "passed"
    await f.settle()
    assert not [c for c in catalog(f) if c.ticket in ("T1", "review:T1")]   # closed
    assert judge.sandbox in f.runner.deleted


@scenario()
async def test_a_message_to_an_agent_that_has_not_started_is_refused(f: Flow):
    await f.to_build()
    impl = f.run("T1", "implementer")
    async with client(f) as c:
        r = await c.post("/a2a/reviewer@review:T1", json=send("What will you check first?"),
                         headers=token(f, impl) | V)
        assert r.status_code == 404                       # it has no card yet
    assert not [x for x in f.ce.runs.values() if x.consult]    # nothing was started to answer


@scenario()
async def test_unknown_skills_are_refused_and_relay_is_gone(f: Flow):
    planner, critic = await to_critique(f)
    async with client(f) as c:
        for body in (send("/relay planner@design hi", wait=True), send("hello", wait=True)):
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
        assert (q.ticket, q.role, q.question) == ("critique", "critic", "May the greeting be in French?")
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
        f.ce.ask_human("t-1", "critique", "critic", critic.id, "Which locale?")
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
        """The catalog holds exactly the change's own cards and one per agent with a sandbox."""
        pushed = sorted(c.id for c in hub.catalog.cards(f.ce.cid))
        agents = {f"{r.role}@{r.ticket}" for r in f.ce.runs.values()
                  if r.sandbox and not r.sandbox_deleted and not r.consult}
        implied = sorted(agents | ({"orchestrator", "resolver"}
                                   if f.ce.change.status == "ACTIVE" else set()))
        assert pushed == implied, (where, pushed, implied)
    return hub, check


@scenario()
async def test_the_catalog_never_drifts_from_the_workflow_through_planning(f: Flow):
    """The cards the workflow pushed (publish as a step starts, withdraw as it ends) are, after
    every step, exactly the cards its state implies; a catalog subscribing later gets the same."""
    from legion_orch.bus import AgentStarted, CardsPublished
    from legion_orch.server.a2a import Catalog
    hub, check = drift_check(f)
    check("start")
    await f.intake()
    check("intake reported")
    await f.signoff("H-1")
    check("H-1")
    await f.plan()
    check("plan")
    await f.critique("REWORK")
    check("critique rework")
    await f.plan()
    await f.critique("PASS")
    check("critique pass")
    late = Catalog()                                  # a catalog subscribing later (a restart)
    f.ctl.bus.subscribe(CardsPublished, lambda e: late.publish(e.cid, list(e.cards)))
    f.ctl.bus.subscribe(AgentStarted, lambda e: late.agent_started(e.cid, e.ticket, e.role,
                                                                    e.sandbox))
    f.ctl.replay_cards()
    assert sorted(c.id for c in late.cards(f.ce.cid)) == sorted(c.id for c in catalog(f))


@scenario()
async def test_the_catalog_never_drifts_from_the_workflow_through_build_and_the_end(f: Flow):
    hub, check = drift_check(f)
    await f.to_build()
    check("build")
    await f.implement()
    check("implemented")
    await f.review(verdict="FAIL")
    check("review fail: rework")
    run = f.run("T1", "implementer")                 # cycle 2: it blocks, it escalates
    out = await f.report(run, [], status="BLOCKED", blocked_reason="the greeting is ambiguous",
                         missing_decision="which language?")
    assert out.accepted, out
    check("escalated")
    await f.change_action("abandon", reason="the goal changed: no greeting needed at all")
    check("abandoned")
    assert hub.catalog.cards(f.ce.cid) == []
