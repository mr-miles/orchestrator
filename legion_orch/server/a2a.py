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
module, deterministic, no model: ``list_cards`` and ``ask_human`` – and one per
**step** whose agent has a sandbox. A message to a step becomes a task that waits for its
agent's answer: delivered to its inbox (MCP ``inbox`` / ``a2a_reply``) while it has a live
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
import re
import uuid
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
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..identity import WorkloadBinding
from ..util import bearer, now_iso
from ..workflow.cards import ORCHESTRATOR, RESOLVER, card_id, card_url, identifier, ordered
from ..workflow.engine import AgentBusy, ChangeEngine, Controller, NoAgent, WorkflowError
from ..workflow.model import Card

log = logging.getLogger("legion.a2a")

MODES = ["text/plain", "application/json"]
SENDS = {"SendMessage", "SendStreamingMessage", "message/send", "message/stream"}
ORCHESTRATOR_SKILLS = [
    AgentSkill(id="list_cards", name="List cards",
               description="The cards of this change you can message: the orchestrator and every "
                           "step whose agent has a sandbox (its roles, and whether it is stopped).",
               tags=["registry"], examples=["/list_cards"]),
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
    """A message waiting for a running step's agent (MCP ``inbox``)."""
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


@dataclass
class QuestionBack:
    """A card answered a message with a question (input-required): the sender's to answer."""
    task_id: str
    card: str                          # who asks
    question: str
    at: str = field(default_factory=now_iso)

    def view(self) -> dict:
        return {"task_id": self.task_id, "from": self.card, "kind": "question",
                "state": "input-required", "text": self.question, "at": self.at,
                "how": "answer with a2a_reply(task_id, text): it continues your conversation "
                       "with " + self.card}


def parse_proposal(answer: str) -> tuple[dict | None, str]:
    """The resolver's proposal from its answer: the last fenced JSON block with a "proposal"
    (None, and why: it proposed none, or none could be read)."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.S)
    for raw in reversed(blocks):
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if isinstance(doc, dict) and "proposal" in doc:
            p = doc["proposal"]
            if p is None:
                return None, str(doc.get("why") or "the resolver proposes nothing")
            if isinstance(p, dict) and isinstance(p.get("action"), str):
                return p, ""
    return None, "no proposal block in its answer: " + answer.strip()[:300]


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


class Catalog:
    """The A2A catalog: every change's published cards, in memory – a view of the workflow,
    pushed to as it goes (``EngineHost.publish_cards`` / ``remove_cards``; given every change's
    cards again when it subscribes). Nothing else holds them."""

    def __init__(self):
        self._cards: dict[str, dict[str, Card]] = {}          # change -> card id -> card

    def publish(self, change: str, cards: list[Card]) -> None:
        mine = self._cards.setdefault(change, {})
        for card in cards:
            mine.setdefault(card.id, card)

    def remove(self, change: str, node: str | None) -> None:
        if node is None:
            self._cards.pop(change, None)
            return
        mine = self._cards.get(change, {})
        for cid in [c.id for c in mine.values() if c.node == node]:
            del mine[cid]

    def cards(self, change: str) -> list[Card]:
        return list(self._cards.get(change, {}).values())

    def get(self, change: str, card: str) -> Card | None:
        return self._cards.get(change, {}).get(card)


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
        self.catalog = Catalog()
        ctl.escalation_listeners.append(self.escalated)       # every escalation: the resolver
        ctl.subscribe_cards(self.catalog)                     # the workflow pushes its cards
        self._dispatchers: dict[tuple[str, str], JsonRpcDispatcher] = {}
        self.inbox: dict[tuple[str, str], dict[str, InboxItem]] = {}
        self.questions: dict[tuple[str, str], dict[str, QuestionBack]] = {}   # sender -> asked

    # ------------------------------------------------------------------ cards
    def url(self, card: str) -> str:
        return card_url(self.ctl.cfg.server.a2a_url, card)

    def cards(self, ce: ChangeEngine) -> list[Card]:
        """The change's cards in the catalog, its own first."""
        return ordered(ce, self.catalog.cards(ce.cid))

    def find(self, ce: ChangeEngine, card_id: str) -> Card | None:
        return self.catalog.get(ce.cid, card_id)

    def agent_card(self, ce: ChangeEngine, card: Card) -> AgentCard:
        """The A2A agent card: who the agent is – no state (that is the step's and its
        tasks')."""
        if card.node is None:
            desc = (f"The orchestrator of change {ce.cid} ({ce.change.goal[:200]}): the controller "
                    "itself, not a model. Lists the change's cards and asks its decision owners. "
                    'Skills are named in a data part {"skill": …} or by a leading /<skill> in the '
                    "text.")
            skills = ORCHESTRATOR_SKILLS
        else:
            desc = (f"The {card.role} agent of step {card.node} in change {ce.cid}: {card.title}. "
                    "Message it at any time: it answers whether or not it is running.")
            skills = [STEP_SKILL]
        return AgentCard(
            name=f"{ce.cid}/{card.id}", description=desc, version=self.ctl.assets.sha[:12] or "1",
            supported_interfaces=[AgentInterface(url=self.url(card.id),
                                                 protocol_binding="JSONRPC",
                                                 protocol_version="1.0")],
            capabilities=AgentCapabilities(streaming=True, push_notifications=False),
            default_input_modes=MODES, default_output_modes=MODES, skills=skills)

    def registry(self, ce: ChangeEngine, caller: WorkloadBinding) -> list[dict]:
        """The caller's change's cards, but its own: who each agent is (its state is the step's:
        MCP step_status)."""
        return [{"id": c.id, "node": c.node, "role": c.role, "title": c.title,
                 "url": self.url(c.id)}
                for c in self.cards(ce) if (c.node, c.role) != (caller.task, caller.role)]

    def ai_catalog(self, ce: ChangeEngine, caller: WorkloadBinding) -> dict:
        """The change's cards as an AI Catalog (what ``a2a catalog add`` registers): one A2A
        agent entry per card the caller may message."""
        entries = []
        for c in self.registry(ce, caller):
            tags = ["orchestrator"] if c["node"] is None else [c["role"], c["node"]]
            entries.append({"identifier": identifier(c["id"]), "displayName": c["id"],
                            "type": "application/a2a-agent-card+json", "url": c["url"],
                            "description": c["title"], "tags": tags})
        return {"specVersion": "1.0",
                "host": {"displayName": f"Legion change {ce.cid}: {ce.change.goal[:120]}"},
                "entries": entries}

    def dispatcher(self, ce: ChangeEngine, card: Card) -> JsonRpcDispatcher:
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
            if ce is None or not self.catalog.cards(cid):
                del self._dispatchers[(cid, card)]

    # ------------------------------------------------------------------ delivery
    # ------------------------------------------------------------------ the resolver
    RESOLVE_TRIES = 3            # an invalid proposal is returned with the reason this often

    def escalated(self, cid: str, node_id: str) -> None:
        """A step escalated: the orchestrator asks the change's resolver (in the background;
        a human may decide first – the proposal is then stale)."""
        ce = self.ctl.changes.get(cid)
        if ce is not None and self.find(ce, RESOLVER) is not None:
            self.ctl.spawn(self.resolve(ce, node_id))

    async def resolve(self, ce: ChangeEngine, node_id: str) -> None:
        brief = ce.escalation_brief(node_id)
        card = self.find(ce, RESOLVER)
        if brief is None or card is None:
            return
        at = brief["escalation"]["at"]
        me = WorkloadBinding(sandbox="controller", change=ce.cid, task=ORCHESTRATOR,
                             role=ORCHESTRATOR)
        text = (f"Step `{node_id}` escalated. Diagnose it and propose one of its `actions`:\n\n"
                f"```json\n{json.dumps(brief, indent=1)}\n```")
        for attempt in range(1, self.RESOLVE_TRIES + 1):
            tid = f"esc-{node_id}-{at}-{attempt}"
            try:
                _, answer = await self.deliver(ce, card, me, text, tid)
            except (Undeliverable, WorkflowError) as e:
                ce.event("resolver", f"could not ask the resolver about {node_id}: {e}", node_id)
                return
            proposal, why = parse_proposal(answer)
            if proposal is None:
                ce.event("resolver", f"no proposal for {node_id}: {why}", node_id)
                return
            err = await ce.propose(node_id, at, proposal)
            if err is None or err == "stale":
                return
            text = (f"Your proposal for `{node_id}` was not taken: {err}. Send it again as the "
                    "fenced JSON block your instructions describe.")

    # ------------------------------------------------------------------ questions back
    def asked_back(self, cid: str, sender: str, task_id: str, card: str, question: str) -> None:
        """``card`` answered the task ``sender`` sent it with a question (input-required): it
        is in the sender's inbox until the sender replies on that task."""
        self.questions.setdefault((cid, sender), {})[task_id] = QuestionBack(
            task_id, card, question)

    def answered_back(self, cid: str, sender: str, task_id: str) -> None:
        self.questions.get((cid, sender), {}).pop(task_id, None)

    def questions_for(self, cid: str, card: str) -> list[QuestionBack]:
        return list(self.questions.get((cid, card), {}).values())

    async def answer_back(self, ce: ChangeEngine, caller: WorkloadBinding, task_id: str,
                          text: str) -> bool:
        """The sender answers a question back (its a2a_reply on that task): the controller
        sends its text as the next message on the task, to the card that asked – as the
        sender would with ``a2a send --task-id``."""
        q = self.questions.get((ce.cid, sender_name(caller)), {}).get(task_id)
        card = self.find(ce, q.card) if q else None
        if q is None or card is None:
            return False
        handler = self.dispatcher(ce, card).request_handler
        msg = Message(message_id=uuid.uuid4().hex, role=Role.ROLE_USER, task_id=task_id,
                      parts=[new_text_part(text)])
        ctx = ServerCallContext(user=CallerUser(caller),
                                state={"caller": caller, "headers": {"A2A-Version": "1.0"}})
        await handler.on_message_send(SendMessageRequest(
            message=msg, configuration=SendMessageConfiguration(return_immediately=True)), ctx)
        return True

    def pending(self, cid: str, card: str) -> list[InboxItem]:
        return list(self.inbox.get((cid, card), {}).values())

    def reply(self, cid: str, card: str, task_id: str, text: str,
              state: str = "completed") -> bool:
        item = self.inbox.get((cid, card), {}).pop(task_id, None)
        if item is None or item.reply.done():
            return False
        item.reply.set_result((state, text))
        return True

    async def deliver(self, ce: ChangeEngine, card: Card, sender: WorkloadBinding, text: str,
                      task_id: str) -> tuple[str, str]:
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
                    return "completed", await ce.a2a_answer(node, role, item.sender, text)
                except AgentBusy:                   # a run started meanwhile: its inbox
                    await asyncio.sleep(0.2)
                except NoAgent as e:
                    raise Undeliverable(str(e)) from None
        finally:
            box.pop(task_id, None)


def _text(updater: TaskUpdater, text: str):
    return updater.new_agent_message([new_text_part(text)])


async def finish(up: TaskUpdater, who: str, state: str, text: str) -> None:
    """The recipient's reply, as the task's state: completed (its answer, an artifact),
    input-required (it asks something back: send again with this task id), rejected."""
    if state == "rejected":
        await up.reject(_text(up, text))
    elif state == "input-required":
        await up.requires_input(_text(up, text))
    else:
        await up.add_artifact([new_text_part(text)], name=f"answer from {who}")
        await up.complete()


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

    def __init__(self, hub: A2AHub, ce: ChangeEngine, card: Card):
        super().__init__(hub, ce)
        self.card = card

    async def run(self, up, task_id, caller, text, data) -> None:
        body = as_text(text, data)
        if not body:
            await up.reject(_text(up, "empty message"))
            return
        sender = sender_name(caller)
        self.hub.answered_back(self.ce.cid, sender, task_id)    # a follow-up: its answer
        await up.start_work(_text(up, f"delivered to {self.card.id}; waiting for its answer"))
        state, answer = await self.hub.deliver(self.ce, self.card, caller, body, task_id)
        if state == "input-required":                            # a question back: the sender's
            self.hub.asked_back(self.ce.cid, sender, task_id, self.card.id, answer)
        await finish(up, self.card.id, state, answer)


HELP = ("Name a skill: a data part {\"skill\": \"list_cards\" | \"ask_human\", …} or text "
        "starting /list_cards or /ask_human <question>. Message another agent at its own card.")


def parse_skill(text: str, data: list[dict]) -> tuple[str, dict[str, Any]]:
    for d in data:
        if isinstance(d.get("skill"), str):
            return d["skill"], d
    if text.startswith("/"):
        head, _, rest = text[1:].partition(" ")
        rest = rest.strip()
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
        elif skill == "ask_human":
            await self.ask_human(up, task_id, caller, str(args.get("question") or "").strip())
        else:
            await up.reject(_text(up, (f"unknown skill {skill!r}. " if skill else "") + HELP))

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

    def card_for(request: Request) -> tuple[ChangeEngine, Card] | Response:
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
        return JSONResponse(hub.ai_catalog(*found))

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
