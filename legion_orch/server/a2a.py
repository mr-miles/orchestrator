"""The agent-to-agent endpoint (A2A v1.0, JSON-RPC; v0.3 accepted): ``server.agent_host:a2a_port``.

Every card is hosted here, by the controller – sandboxes make outbound calls only:

* ``GET  /a2a/cards``              the caller's change's cards (the registry)
* ``GET  /a2a/ai-catalog.json``     the same as an AI Catalog (``a2a catalog add``, at sandbox
  start; ``a2a catalog update`` picks up cards registered since)
* ``GET  /a2a/<card>``              a card (also at ``…/.well-known/agent-card.json``)
* ``POST /a2a/<card>``              its JSON-RPC endpoint

The URLs name no change: each request is answered for the caller's own change, which its
identity names.

The cards are those of :mod:`legion_orch.workflow.cards`: the change's **orchestrator** – this
module, deterministic, no model: ``list_cards``, ``relay`` and ``ask_human`` – and one per
**step** whose agent has a sandbox. A message to a step becomes a task that waits for its
agent's answer: delivered to its inbox (MCP ``a2a_inbox`` / ``a2a_reply``) while it has a live
run, otherwise answered in one resumed turn of its session, its sandbox started first if it was
stopped (``engine/sandboxes/answer.py``).

Every request carries the bearer the OpenShell gateway injects (``legion-a2a`` provider
profile): it names the calling sandbox's change, task and role, and a caller sees and reaches
only its own change. Who sent a message is that binding – never what the message says. Tasks live
in memory, scoped to the agent that created them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import weakref
from dataclasses import dataclass, field
from typing import Any

from a2a.auth.user import User
from a2a.helpers.proto_helpers import (
    get_data_parts,
    get_text_parts,
    new_data_part,
    new_task_from_user_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..identity import WorkloadBinding
from ..util import bearer, now_iso
from ..workflow.cards import StepCard, card_id, card_url, identifier
from ..workflow.engine import AgentBusy, ChangeEngine, Controller, NoAgent, WorkflowError

log = logging.getLogger("legion.a2a")

MODES = ["text/plain", "application/json"]
SENDS = {"SendMessage", "SendStreamingMessage", "message/send", "message/stream"}
ORCHESTRATOR_SKILLS = [
    AgentSkill(id="list_cards", name="List cards",
               description="The cards of this change you can message: the orchestrator and every "
                           "step whose agent has a sandbox (its roles, and whether it is stopped).",
               tags=["registry"], examples=["/list_cards"]),
    AgentSkill(id="relay", name="Relay a message",
               description="Pass a message to a step's agent and return its answer. A step's card "
                           "takes messages directly too; relay is for a step you know only by id.",
               tags=["messaging"],
               examples=["/relay build:T2 Which module owns the config loader?",
                         '{"skill": "relay", "to": "build:T2", "text": "…"}']),
    AgentSkill(id="ask_human", name="Ask a human",
               description="Ask the change's decision owners a question. The task completes with "
                           "their answer, which can take hours: send with --async and poll. Ask "
                           "only what your assignment and the documents cannot answer.",
               tags=["human"],
               examples=["/ask_human Should the CLI keep the deprecated --legacy flag?"]),
]
STEP_SKILL = AgentSkill(
    id="message", name="Message the step's agent",
    description="Ask this step's agent about its work. It answers when it next checks its inbox "
                "(while running), or in a turn of its session started for the message.",
    tags=["messaging"])


def sender_name(b: WorkloadBinding) -> str:
    """The sender as its card id (``<role>@<node>``)."""
    return card_id(b.role, b.task)


class CallerUser(User):
    """The calling agent, as the SDK's task owner: tasks are visible only to who created them."""

    def __init__(self, b: WorkloadBinding):
        self.b = b

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return f"{self.b.change}/{self.b.task}/{self.b.role}/{self.b.sandbox}"


class CallerContext(DefaultServerCallContextBuilder):
    """The SDK's call context (headers, extensions), its user the binding :func:`build_a2a_app`
    checked for the request."""

    def build(self, request: Request) -> ServerCallContext:
        ctx = super().build(request)
        ctx.state["caller"] = request.state.caller
        return ctx

    def build_user(self, request: Request) -> User:
        return CallerUser(request.state.caller)


@dataclass
class InboxItem:
    """A message waiting for a running step's agent (MCP ``a2a_inbox``)."""
    task_id: str
    node: str
    sender: str
    sender_role: str
    text: str
    at: str = field(default_factory=now_iso)
    reply: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def view(self) -> dict:
        return {"task_id": self.task_id, "from": self.sender, "from_role": self.sender_role,
                "text": self.text, "at": self.at}


class Undeliverable(Exception):
    """The recipient cannot answer any more (its sandbox is gone)."""


def message_input(context: RequestContext) -> tuple[str, list[dict]]:
    msg = context.message
    if msg is None:
        return "", []
    text = "\n".join(get_text_parts(msg.parts)).strip()
    data = [d for d in get_data_parts(msg.parts) if isinstance(d, dict)]
    return text, data


def as_text(text: str, data: list[dict]) -> str:
    return "\n".join([text] + [json.dumps(d, indent=1) for d in data]).strip()


class A2AHub:
    """The A2A side of a controller: each card's SDK handler, and the inboxes of running steps.
    One per controller (:meth:`of`), shared by the A2A and MCP listeners."""

    WAIT_S = 5.0                   # longest wait between looks at a recipient's state

    _hubs: weakref.WeakKeyDictionary[Controller, A2AHub] = weakref.WeakKeyDictionary()

    @classmethod
    def of(cls, ctl: Controller) -> A2AHub:
        hub = cls._hubs.get(ctl)
        if hub is None:
            hub = cls._hubs[ctl] = cls(ctl)
        return hub

    def __init__(self, ctl: Controller):
        self.ctl = ctl
        self._dispatchers: dict[tuple[str, str], JsonRpcDispatcher] = {}
        self.inbox: dict[tuple[str, str], dict[str, InboxItem]] = {}

    # ------------------------------------------------------------------ cards
    def url(self, card: str) -> str:
        return card_url(self.ctl.cfg.server.a2a_url, card)

    def find(self, ce: ChangeEngine, card_id: str) -> StepCard | None:
        return next((c for c in ce.a2a_cards() if c.id == card_id), None)

    def agent_card(self, ce: ChangeEngine, card: StepCard) -> AgentCard:
        if card.node is None:
            desc = (f"The orchestrator of change {ce.cid} ({ce.change.goal[:200]}): the controller "
                    "itself, not a model. Lists the change's cards, relays messages to its steps "
                    "and asks its decision owners. Skills are named in a data part "
                    '{"skill": …} or by a leading /<skill> in the text.')
            skills = ORCHESTRATOR_SKILLS
        else:
            desc = (f"The {card.role} agent of step {card.node} in change {ce.cid}: {card.title}."
                    + {"not started": " It has not started: a message starts it to answer.",
                       "stopped": " Its sandbox is stopped: a message starts it."}.get(card.state, ""))
            skills = [STEP_SKILL]
        return AgentCard(
            name=f"{ce.cid}/{card.id}", description=desc, version=self.ctl.assets.sha[:12] or "1",
            supported_interfaces=[AgentInterface(url=self.url(card.id),
                                                 protocol_binding="JSONRPC",
                                                 protocol_version="1.0")],
            capabilities=AgentCapabilities(streaming=True, push_notifications=False),
            default_input_modes=MODES, default_output_modes=MODES, skills=skills)

    def registry(self, ce: ChangeEngine, caller: WorkloadBinding) -> list[dict]:
        """The caller's change's cards, but its own step's."""
        out = []
        for c in ce.a2a_cards():
            if (c.node, c.role) == (caller.task, caller.role):
                continue
            state = "orchestrator" if c.node is None else ce.agent_state(c.node, c.role) or "gone"
            out.append({"id": c.id, "node": c.node, "role": c.role, "title": c.title,
                        "stopped": c.stopped, "state": state, "url": self.url(c.id)})
        return out

    def catalog(self, ce: ChangeEngine, caller: WorkloadBinding) -> dict:
        """The change's cards as an AI Catalog (what ``a2a catalog add`` registers): one A2A
        agent entry per card the caller may message."""
        entries = []
        for c in self.registry(ce, caller):
            tags = ["orchestrator"] if c["node"] is None else [c["role"], c["node"]]
            entries.append({"identifier": identifier(c["id"]), "displayName": c["id"],
                            "type": "application/a2a-agent-card+json", "url": c["url"],
                            "description": c["title"] + (f" ({c['state']})"
                                                         if c["state"] in ("absent", "idle") else ""),
                            "tags": tags + (["stopped"] if c["stopped"] else [])
                            + (["not-started"] if c["state"] == "absent" else [])})
        return {"specVersion": "1.0",
                "host": {"displayName": f"Legion change {ce.cid}: {ce.change.goal[:120]}"},
                "entries": entries}

    def dispatcher(self, ce: ChangeEngine, card: StepCard) -> JsonRpcDispatcher:
        key = (ce.cid, card.id)
        d = self._dispatchers.get(key)
        if d is None:
            executor = (OrchestratorExecutor(self, ce) if card.node is None
                        else StepExecutor(self, ce, card))
            handler = DefaultRequestHandlerV2(executor, InMemoryTaskStore(),
                                              self.agent_card(ce, card))
            d = self._dispatchers[key] = JsonRpcDispatcher(handler, CallerContext(),
                                                           enable_v0_3_compat=True)
        self._prune()
        return d

    def retired(self, ce: ChangeEngine, card_id: str) -> JsonRpcDispatcher | None:
        """A removed card's handler: its senders may still look their tasks up (until the change
        ends), but it takes no new message."""
        return self._dispatchers.get((ce.cid, card_id))

    def _prune(self) -> None:
        """Forget the handlers (and their tasks) of changes that are no longer active."""
        for cid, card in list(self._dispatchers):
            ce = self.ctl.changes.get(cid)
            if ce is None or not ce.a2a_cards():
                del self._dispatchers[(cid, card)]

    # ------------------------------------------------------------------ delivery
    def pending(self, cid: str, card: str) -> list[InboxItem]:
        return list(self.inbox.get((cid, card), {}).values())

    def reply(self, cid: str, card: str, task_id: str, text: str) -> bool:
        item = self.inbox.get((cid, card), {}).pop(task_id, None)
        if item is None or item.reply.done():
            return False
        item.reply.set_result(text)
        return True

    async def deliver(self, ce: ChangeEngine, card: StepCard, sender: WorkloadBinding, text: str,
                      task_id: str) -> str:
        """``text`` from ``sender`` to the step agent of ``card``; its answer. While the step has a live
        run the message waits in its inbox; when it has none (also once a run ends without
        answering) one resumed turn of its session answers. Raises :class:`Undeliverable` once
        the step has no sandbox."""
        node, role = card.node, card.role
        box = self.inbox.setdefault((ce.cid, card.id), {})
        item = InboxItem(task_id, card.id, sender_name(sender), sender.role, text)
        try:
            while True:
                state = ce.agent_state(node, role)
                if state is None:
                    raise Undeliverable(f"{card.id} has no sandbox any more")
                if state == "live":
                    if task_id not in box and not item.reply.done():
                        box[task_id] = item
                        ce.event("a2a", f"message from {item.sender} waits in {card.id}'s "
                                        "inbox", node)
                    saved = asyncio.ensure_future(ce.next_save())
                    try:
                        await asyncio.wait({item.reply, saved}, timeout=self.WAIT_S,
                                           return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        saved.cancel()
                    if item.reply.done():
                        return item.reply.result()
                    continue
                box.pop(task_id, None)              # idle: its agent answers in a turn
                if item.reply.done():
                    return item.reply.result()
                try:
                    return await ce.a2a_answer(node, role, item.sender, text)
                except AgentBusy:                   # a run started meanwhile: its inbox
                    await asyncio.sleep(0.2)
                except NoAgent as e:
                    raise Undeliverable(str(e)) from None
        finally:
            box.pop(task_id, None)


def _text(updater: TaskUpdater, text: str):
    return updater.new_agent_message([new_text_part(text)])


class _Executor(AgentExecutor):
    def __init__(self, hub: A2AHub, ce: ChangeEngine):
        self.hub, self.ce = hub, ce

    async def execute(self, context: RequestContext, event_queue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue, task.id, task.context_id)
        caller: WorkloadBinding = context.call_context.state["caller"]
        text, data = message_input(context)
        try:
            await self.run(up, task.id, caller, text, data)
        except (Undeliverable, WorkflowError) as e:
            await up.failed(_text(up, str(e)))

    async def cancel(self, context: RequestContext, event_queue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    async def run(self, up: TaskUpdater, task_id: str, caller: WorkloadBinding, text: str,
                  data: list[dict]) -> None:
        raise NotImplementedError


class StepExecutor(_Executor):
    """A step's card: the message waits for its agent's answer."""

    def __init__(self, hub: A2AHub, ce: ChangeEngine, card: StepCard):
        super().__init__(hub, ce)
        self.card = card

    async def run(self, up, task_id, caller, text, data) -> None:
        body = as_text(text, data)
        if not body:
            await up.reject(_text(up, "empty message"))
            return
        await up.start_work(_text(up, f"delivered to {self.card.id}; waiting for its answer"))
        answer = await self.hub.deliver(self.ce, self.card, caller, body, task_id)
        await up.add_artifact([new_text_part(answer)], name=f"answer from {self.card.id}")
        await up.complete()


HELP = ("Name a skill: a data part {\"skill\": \"list_cards\" | \"relay\" | \"ask_human\", …} or "
        "text starting /list_cards, /relay <card> <message>, /ask_human <question>.")


def parse_skill(text: str, data: list[dict]) -> tuple[str, dict[str, Any]]:
    for d in data:
        if isinstance(d.get("skill"), str):
            return d["skill"], d
    if text.startswith("/"):
        head, _, rest = text[1:].partition(" ")
        rest = rest.strip()
        if head == "relay":
            to, _, msg = rest.partition(" ")
            return head, {"to": to, "text": msg.strip()}
        if head == "ask_human":
            return head, {"question": rest}
        return head, {}
    return "", {}


class OrchestratorExecutor(_Executor):
    """The change's orchestrator: deterministic skills over the change's cards and its humans."""

    async def run(self, up, task_id, caller, text, data) -> None:
        skill, args = parse_skill(text, data)
        if skill == "list_cards":
            await up.add_artifact([new_data_part({"cards": self.hub.registry(self.ce, caller)})],
                                  name="cards")
            await up.complete()
        elif skill == "relay":
            await self.relay(up, task_id, caller, str(args.get("to") or ""),
                             str(args.get("text") or "").strip())
        elif skill == "ask_human":
            await self.ask_human(up, task_id, caller, str(args.get("question") or "").strip())
        else:
            await up.reject(_text(up, (f"unknown skill {skill!r}. " if skill else "") + HELP))

    async def relay(self, up, task_id, caller, to: str, body: str) -> None:
        card = self.hub.find(self.ce, to)
        if card is None or card.node is None:
            await up.reject(_text(up, f"{to!r} is not a step card of change {self.ce.cid} "
                                      "(see /list_cards)"))
            return
        if (card.node, card.role) == (caller.task, caller.role):
            await up.reject(_text(up, "that is your own card"))
            return
        if not body:
            await up.reject(_text(up, "relay needs a message"))
            return
        await up.start_work(_text(up, f"relayed to {to}; waiting for its answer"))
        answer = await self.hub.deliver(self.ce, card, caller, body, task_id)
        await up.add_artifact([new_text_part(answer)], name=f"answer from {to}")
        await up.complete()

    async def ask_human(self, up, task_id, caller, question: str) -> None:
        ce = self.ce
        if not question:
            await up.reject(_text(up, "ask_human needs a question"))
            return
        async with ce.lock:
            run = next((r.id for r in reversed(list(ce.runs.values()))
                        if r.sandbox_ref == caller.sandbox), None)
            q = ce.ask_human(task_id, caller.task, caller.role, run, question)
        await up.start_work(_text(up, "asked the change's decision owners; waiting for an answer"))
        try:
            while True:
                if q.answer is not None:
                    await up.add_artifact([new_text_part(q.answer)],
                                          name=f"answer from {q.answered_by}")
                    await up.complete()
                    return
                if q.withdrawn is not None:
                    await up.failed(_text(up, f"question withdrawn: {q.withdrawn}"))
                    return
                if ce.agent_state(caller.task, caller.role) is None:
                    async with ce.lock:
                        ce.withdraw_question(q.id, "the asking agent's sandbox is gone")
                    continue
                saved = asyncio.ensure_future(ce.next_save())
                try:
                    await asyncio.wait({saved}, timeout=A2AHub.WAIT_S)
                finally:
                    saved.cancel()
        except asyncio.CancelledError:
            async with ce.lock:
                ce.withdraw_question(q.id, "the asking agent cancelled it")
            raise


def build_a2a_app(ctl: Controller) -> Starlette:
    hub = A2AHub.of(ctl)

    def check(request: Request) -> tuple[ChangeEngine, WorkloadBinding] | Response:
        found = ctl.a2a_caller(bearer(request.headers.get("authorization", "")))
        if found is None:
            return JSONResponse({"error": "no agent sandbox for this identity (attach the "
                                          "legion-a2a provider)"}, status_code=401)
        return found

    def card_for(request: Request) -> tuple[ChangeEngine, StepCard] | Response:
        """The caller's change's card named in the path (the change is the bearer's)."""
        found = check(request)
        if isinstance(found, Response):
            return found
        ce, caller = found
        request.state.caller, request.state.ce = caller, ce
        card = hub.find(ce, request.path_params["card"])
        if card is None:
            return JSONResponse({"error": "no such card in your change (GET /a2a/cards)"},
                                status_code=404)
        return ce, card

    async def cards(request: Request) -> Response:
        found = check(request)
        if isinstance(found, Response):
            return found
        ce, caller = found
        return JSONResponse({"change": ce.cid, "you": card_id(caller.role, caller.task),
                             "cards": hub.registry(ce, caller)})

    async def catalog(request: Request) -> Response:
        found = check(request)
        if isinstance(found, Response):
            return found
        return JSONResponse(hub.catalog(*found))

    async def card(request: Request) -> Response:
        found = card_for(request)
        if isinstance(found, Response):
            return found
        ce, c = found
        return JSONResponse(agent_card_to_dict(hub.agent_card(ce, c)))

    async def rpc(request: Request) -> Response:
        found = card_for(request)
        if isinstance(found, Response):
            ce = getattr(request.state, "ce", None)
            old = hub.retired(ce, request.path_params["card"]) if ce is not None else None
            if old is None or await sends(request):
                return found
            return await old.handle_requests(request)     # a task of a removed card
        ce, c = found
        return await hub.dispatcher(ce, c).handle_requests(request)

    async def sends(request: Request) -> bool:
        try:
            body = await request.json()
        except ValueError:
            return True
        return not isinstance(body, dict) or body.get("method") in SENDS

    # One set of URLs for every change: what a caller sees and reaches is its own change's,
    # by its identity (the bearer), never by anything in the request.
    return Starlette(routes=[
        Route("/a2a/cards", cards, methods=["GET"]),
        Route("/a2a/ai-catalog.json", catalog, methods=["GET"]),
        Route("/a2a/.well-known/ai-catalog.json", catalog, methods=["GET"]),
        Route("/a2a/{card}", card, methods=["GET"]),
        Route("/a2a/{card}/.well-known/agent-card.json", card, methods=["GET"]),
        Route("/a2a/{card}", rpc, methods=["POST"]),
    ])


__all__ = ["A2AHub", "build_a2a_app"]
