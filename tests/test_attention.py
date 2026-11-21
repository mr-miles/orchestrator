"""Attention items and notifications (WORKFLOW.md §2.2): what needs a decision owner, the
inbox, the notifier's sinks, reminders, restarts, and the operator API and CLI that show them."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import textwrap

import httpx
import pytest
from engine_harness import REPO, Flow, scenario

from legion_orch import cli
from legion_orch.config import ATTENTION_KINDS, NotifyConfig, NotifySink, load_config
from legion_orch.notify import Notifier
from legion_orch.workflow import attention
from legion_orch.workflow.engine import Controller
from legion_orch.workflow.model import AWAITING_HUMAN, ESCALATED, ChangeStatus


def _events(f: Flow, kind: str) -> list[dict]:
    return [e for e in f.ce.timeline_events() if e["kind"] == kind]


def _recorder(tmp_path) -> tuple[NotifySink, object]:
    """A command sink appending each notification (one JSON line) to a file."""
    out = tmp_path / "notified.jsonl"
    code = ("import sys, pathlib; p = pathlib.Path(sys.argv[1]); "
            "p.open('a').write(sys.stdin.read().strip() + '\\n')")
    return NotifySink(kind="command", argv=[sys.executable, "-c", code, str(out)]), out


def _sent(out) -> list[dict]:
    return [json.loads(x) for x in out.read_text().splitlines()] if out.exists() else []


async def _flow_with(tmp_path, *sinks: NotifySink, **notify) -> Flow:
    """``Flow.create`` with notification sinks configured before the change starts."""
    f = Flow(tmp_path)
    f.cfg.notify.sinks = list(sinks)
    for k, v in notify.items():
        setattr(f.cfg.notify, k, v)
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change("Say hello", REPO)
    await f.settle()
    return f


def run(coro):
    return asyncio.run(coro)


# ====================================================================== items
@scenario()
async def test_a_gate_is_raised_once_and_resolved_with_its_wait(f: Flow):
    assert f.ce.attention_items == []
    await f.intake()
    assert f.status("H-1") == AWAITING_HUMAN
    [item] = f.ce.attention_items
    assert (item.kind, item.node, item.blocking) == ("gate", "H-1", True)
    assert item.since == f.ce.nodes["H-1"].status_since and item.since
    f.ce.save()                                   # saving again raises nothing new
    assert [e["message"] for e in _events(f, "attention")] == [f"raised: {item.title}"]
    await f.human("H-1", "accept")
    resolved = [e["message"] for e in _events(f, "attention") if e["message"].startswith("resolved")]
    assert len(resolved) == 1 and resolved[0].endswith(item.title) and " after " in resolved[0]
    assert not any(i.node == "H-1" for i in f.ce.attention_items)


@scenario()
async def test_an_escalation_is_an_item_with_its_reason(f: Flow):
    await f.exit_agent(f.run("intake"), rc=1)           # the agent died: run_failed
    assert f.status("intake") == ESCALATED
    [item] = f.ce.attention_items
    esc = f.ce.nodes["intake"].escalation
    assert (item.kind, item.detail, item.since, item.blocking) == ("escalation", esc.kind, esc.at, True)
    assert item.reason and len(item.reason) <= attention.REASON_MAX
    await f.human("intake", "retry")
    assert f.ce.attention_items == []


@scenario()
async def test_an_ended_change_goes_to_the_inbox(f: Flow):
    await f.exit_agent(f.run("intake"), rc=1)
    await f.human("intake", "abandon", reason="the goal was a duplicate of another change")
    assert f.ce.change.status == ChangeStatus.ABANDONED
    [item] = [i for i in f.ce.attention_items if i.kind == attention.CHANGE_ENDED]
    assert not item.blocking and item.detail == "ABANDONED" and "duplicate" in item.reason
    assert not any(i.blocking for i in f.ce.attention_items)


@scenario()
async def test_a_shipped_change_and_its_retrospective_go_to_the_inbox(f: Flow):
    await f.to_build("R1")
    await f.implement()
    await f.review(verdict="PASS")
    assert f.ce.change.status == ChangeStatus.READY
    [ended] = f.ce.attention_items
    assert (ended.kind, ended.detail, ended.node, ended.blocking) == \
        ("change_ended", "READY", "H-3", False)
    await f.change_action("retrospective")
    run_ = f.run("retrospective")
    doc = await f.write(run_, "retro/report.md", "# Retrospective\n\n## Summary\n\nFine.\n")
    await f.report(run_, [doc])
    retro = [i for i in f.ce.attention_items if i.kind == attention.RETROSPECTIVE]
    assert [(i.node, i.detail, i.blocking) for i in retro] == [("retrospective", "cycle-1", False)]
    await f.change_action("retrospective")           # asked again: the next report is a new item
    assert not [i for i in f.ce.attention_items if i.kind == attention.RETROSPECTIVE]


def test_attention_kinds_match_the_config():
    assert ATTENTION_KINDS == attention.KINDS


# ====================================================================== notifications
def test_a_gate_notifies_its_sinks_once_and_records_the_delivery(tmp_path):
    async def go():
        sink, out = _recorder(tmp_path)
        f = await _flow_with(tmp_path, sink, ui_url="https://legion.example/")
        await f.intake()
        [msg] = _sent(out)
        assert (msg["event"], msg["kind"], msg["node"], msg["change"]) == \
            ("raised", "gate", "H-1", f.ce.cid)
        assert msg["url"] == f"https://legion.example/#{f.ce.cid}/overview/H-1"
        assert msg["project"] == "engine-test" and msg["approvers"] == ["miles", "ana"]
        assert [e for e in _events(f, "notified") if "sent" in e["message"]]
        await f.human("H-1", "accept")
        assert len(_sent(out)) == 1                  # resolved is off by default
    run(go())


def test_inbox_items_are_sent_only_to_sinks_that_ask_for_them(tmp_path):
    async def go():
        sink, out = _recorder(tmp_path)
        sink.items = ["change_ended"]
        f = await _flow_with(tmp_path, sink)
        await f.exit_agent(f.run("intake"), rc=1)     # an escalation: not this sink's kind
        assert _sent(out) == []
        await f.human("intake", "abandon")
        [msg] = _sent(out)
        assert msg["kind"] == "change_ended" and msg["blocking"] is False
    run(go())


def test_a_failing_sink_is_retried_then_recorded_without_disturbing_the_change(tmp_path, monkeypatch):
    monkeypatch.setattr(Notifier, "RETRY_DELAYS_S", (0.0, 0.0))
    attempts = tmp_path / "attempts"
    code = f"import sys; open({str(attempts)!r}, 'a').write('x'); sys.exit(3)"
    bad = NotifySink(kind="command", argv=[sys.executable, "-c", code], name="pager")

    async def go():
        f = await _flow_with(tmp_path, bad)
        await f.intake()
        assert f.status("H-1") == AWAITING_HUMAN
        assert attempts.read_text() == "xxx"
        [ev] = _events(f, "notified")
        assert "raised via pager: failed: RuntimeError: exit 3" in ev["message"]
    run(go())


def test_resolved_notifications_when_configured(tmp_path):
    async def go():
        sink, out = _recorder(tmp_path)
        f = await _flow_with(tmp_path, sink, resolved=True)
        await f.intake()
        await f.human("H-1", "accept")
        assert [m["event"] for m in _sent(out)] == ["raised", "resolved"]
    run(go())


def test_reminders_repeat_blocking_items_only():
    sent: list[tuple[str, str]] = []
    cfg = NotifyConfig(remind_after_s=60, items=list(ATTENTION_KINDS),
                       sinks=[NotifySink(kind="command", argv=["x"])])
    n = Notifier(cfg, "p", ["miles"], spawn=lambda c: c.close(), on_result=lambda *a: None)
    n._send = lambda item, event: sent.append((item.node, event))
    gate = attention.AttentionItem(change="c1", goal="g", node="H-2", kind="gate", detail="H-2",
                                   title="H-2", since="t", blocking=True)
    inbox = attention.AttentionItem(change="c1", goal="g", node=None, kind="change_ended",
                                    detail="READY", title="done", since="t", blocking=False)
    n.raised(gate)
    n.raised(inbox)
    n.remind([gate, inbox], now=1e12)
    n.remind([gate, inbox], now=1e12 + 30)            # not yet due again
    n.remind([gate, inbox], now=1e12 + 61)
    assert sent == [("H-2", "raised"), (None, "raised"), ("H-2", "reminder"), ("H-2", "reminder")]
    n.resolved(gate)
    n.remind([inbox], now=1e12 + 1000)
    assert len(sent) == 4


def test_a_restart_does_not_raise_or_notify_again(tmp_path):
    async def go():
        sink, out = _recorder(tmp_path)
        f = await _flow_with(tmp_path, sink)
        await f.intake()
        assert len(_sent(out)) == 1
        f.ce.flush_timeline(force=True)
        ctl2 = Controller(f.cfg, runner=f.runner)
        ctl2.load()
        ce2 = ctl2.changes[f.ce.cid]
        assert [i.key for i in ce2.attention_items] == [i.key for i in f.ce.attention_items]
        assert ce2.nodes["H-1"].status_since == f.ce.nodes["H-1"].status_since
        async with ctl2.svc.lock:
            ce2.recover()                              # saves
        await asyncio.sleep(0.2)
        assert len(_sent(out)) == 1
        assert len([e for e in ce2.timeline_events() if e["kind"] == "attention"]) == 1
        ctl2.notifier.remind(ctl2.attention_items())   # seeded at start-up: not due yet
        await asyncio.sleep(0.2)
        assert len(_sent(out)) == 1
    run(go())


# ====================================================================== sinks over HTTP
class _Server:
    """A minimal HTTP server recording each request (headers and body)."""

    def __init__(self):
        self.requests: list[dict] = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        lines = head.split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:] if h)}
        body = await reader.readexactly(int(headers.get("content-length", 0)))
        self.requests.append({"line": lines[0], "headers": headers, "body": body})
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
        await writer.drain()
        writer.close()


def test_webhook_and_ntfy_sinks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", "s3cret")

    async def go():
        async with _Server() as srv:
            hook = NotifySink(kind="webhook", url=f"{srv.url}/hook", secret_env="HOOK_SECRET")
            ntfy = NotifySink(kind="ntfy", url=f"{srv.url}/legion-topic")
            f = await _flow_with(tmp_path, hook, ntfy, ui_url="https://ui/")
            await f.intake()
            for _ in range(50):
                if len(srv.requests) == 2:
                    break
                await asyncio.sleep(0.05)
            by_path = {r["line"].split()[1]: r for r in srv.requests}
            wh = by_path["/hook"]
            want = "sha256=" + hmac.new(b"s3cret", wh["body"], hashlib.sha256).hexdigest()
            assert wh["headers"]["x-legion-signature"] == want
            assert json.loads(wh["body"])["node"] == "H-1"
            nt = json.loads(by_path["/"]["body"])
            assert nt["topic"] == "legion-topic" and nt["priority"] == 4
            assert nt["click"] == f"https://ui/#{f.ce.cid}/overview/H-1"
            assert nt["title"].startswith("H-1: ")
    run(go())


# ====================================================================== config
@pytest.mark.parametrize("notify, error", [
    ({"items": ["gate", "nope"]}, "unknown item kinds"),
    ({"sinks": [{"kind": "email"}]}, "is not one of"),
    ({"sinks": [{"kind": "webhook"}]}, "url must be"),
    ({"sinks": [{"kind": "command"}]}, "argv is required"),
    ({"sinks": [{"kind": "ntfy", "url": "https://ntfy.sh/t", "colour": 1}]}, "unknown NotifySink keys"),
])
def test_notify_config_is_validated(tmp_path, notify, error):
    (tmp_path / "c.yaml").write_text(textwrap.dedent(f"""
        project: p
        workspaces: [{{name: app}}]
        decision_owners: [miles]
        sandbox: {{runner: local}}
        notify: {json.dumps(notify)}
    """))
    with pytest.raises(ValueError, match=error):
        load_config(tmp_path / "c.yaml")


# ====================================================================== API and CLI
@scenario()
async def test_operator_api_lists_attention_and_digest(f: Flow):
    from legion_orch.server.web import build_app
    await f.intake()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(f.ctl)),
                                 base_url="http://op") as c:
        a = (await c.get("/api/attention")).json()
        assert [i["node"] for i in a["needs_you"]] == ["H-1"] and a["inbox"] == []
        assert a["needs_you"][0]["key"]
        [summary] = (await c.get("/api/changes")).json()
        assert summary["needs_you"] == 1
        assert (await c.get("/api/digest")).status_code == 400
        d = (await c.get("/api/digest", params={"since": "2000-01-01T00:00:00.000Z"})).json()
        [ch] = d["changes"]
        assert ch["id"] == f.ce.cid and ch["counts"]["runs started"] == 1
        assert [i["node"] for i in d["needs_you"]] == ["H-1"]
        d = (await c.get("/api/digest", params={"since": "2999-01-01T00:00:00.000Z"})).json()
        assert d["changes"] == [] and len(d["needs_you"]) == 1      # waiting items always shown


def test_cli_attention_prints_items_with_links(tmp_path, monkeypatch, capsys):
    item = {"change": "c1", "node": "H-2", "title": "H-2: Approve spec & oracle",
            "since": "2026-11-18T00:33:00.000Z", "reason": "", "blocking": True}
    monkeypatch.setattr(cli.OperatorClient, "attention",
                        lambda self: {"needs_you": [item], "inbox": []})
    (tmp_path / "c.yaml").write_text("project: p\nworkspaces: [{name: app}]\ndecision_owners: [m]\n"
                                     "sandbox: {runner: local}\nnotify: {ui_url: 'https://ui/'}\n")
    assert cli.main(["-c", str(tmp_path / "c.yaml"), "attention"]) == 0
    out = capsys.readouterr().out
    assert "Needs you (1)" in out and "https://ui/#c1/overview/H-2" in out and "Inbox (0)" in out


def test_cli_digest_since_accepts_durations():
    assert cli._since("2026-11-18T00:00:00.000Z") == "2026-11-18T00:00:00.000Z"
    assert cli._since("8h").endswith("Z") and cli._since("8h") < cli._since("1m")


def test_notify_config_from_yaml(tmp_path):
    base = "project: p\nworkspaces: [{name: app}]\ndecision_owners: [m]\nsandbox: {runner: local}\n"
    (tmp_path / "c.yaml").write_text(base + textwrap.dedent("""
        notify:
          items: [gate, escalation, change_ended]
          sinks:
            - kind: ntfy
              url: https://ntfy.sh/t
              items: [retrospective]
    """))
    n = load_config(tmp_path / "c.yaml").notify
    assert n.items == ["gate", "escalation", "change_ended"] and n.sinks[0].items == ["retrospective"]
    (tmp_path / "c.yaml").write_text(base + "notify:\n  on: [gate]\n")
    with pytest.raises(ValueError, match="the key is `items:`"):
        load_config(tmp_path / "c.yaml")
