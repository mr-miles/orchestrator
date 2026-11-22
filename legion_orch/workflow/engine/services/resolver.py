"""The resolver (WORKFLOW.md §5.16): every escalation goes to the change's resolver agent, which
diagnoses it and proposes one of the step's human actions; a human applies, edits or ignores it.

When a step escalates (a failure – not a gate's due decision; ``ChangeCore`` notices it as the
change saves) the resolver is sent a message through the inboxes – the escalation, the step's
state, the actions a human may take – and its answer's proposal, a fenced JSON block, is
recorded on the escalation (and at ``control/proposals/<node>-<n>.json``). The message goes
through the A2A layer's inbox (``EngineHost.deliver``), as any other. A proposal naming an
action not offered is returned with the reason (:data:`TRIES`); one for an escalation decided
first is stale.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable

from ... import cards, context
from ...model import ESCALATED, ResolverProposal
from ..base.common import NoAgent, WorkflowError, _slug
from ..base.core import ChangeCore

TRIES = 3


def parse_proposal(answer: str) -> tuple[dict | None, str]:
    """The resolver's proposal from its answer: the last fenced JSON block with a "proposal"
    (None, and why: it proposed none, or none could be read)."""
    for raw in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.S)):
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


class Resolver:
    def __init__(self, core: ChangeCore, actions: Callable[[str], list[str]]):
        self.c, self.actions = core, actions

    def brief(self, node_id: str) -> dict | None:
        """What the resolver is told of an escalation (None: the step is not escalated)."""
        node = self.c.nodes.get(node_id)
        if node is None or node.status != ESCALATED or node.escalation is None:
            return None
        esc = node.escalation
        return {"node": node.id, "title": node.title, "escalation": {
                    "kind": esc.kind, "reason": esc.reason, "at": esc.at, "run": esc.run,
                    "missing_decision": esc.missing_decision,
                    "log_tail": (esc.log_tail or "")[-4000:],
                    "source": str(esc.source) if esc.source else None},
                "actions": self.actions(node_id),
                "step": context.step_status(self.c, node_id)}

    async def resolve(self, node_id: str) -> None:
        """Ask the resolver about the escalation of ``node_id``; record its proposal."""
        async with self.c.lock:
            brief = self.brief(node_id)
        card = next((x for x in self.c.a2a_cards() if x.id == cards.RESOLVER), None)
        if brief is None or card is None:
            return
        at = brief["escalation"]["at"]
        text = (f"Step `{node_id}` escalated. Diagnose it and propose one of its `actions`:\n\n"
                f"```json\n{json.dumps(brief, indent=1)}\n```")
        for attempt in range(1, TRIES + 1):
            try:
                _, answer = await self.c.host.deliver(self.c.cid, card.id, cards.ORCHESTRATOR,
                                                      cards.ORCHESTRATOR, text,
                                                      f"esc-{node_id}-{at}-{attempt}")
            except (NoAgent, WorkflowError) as e:
                async with self.c.lock:
                    self.c.event("resolver", f"could not ask the resolver about {node_id}: {e}",
                                 node_id)
                    self.c.save()
                return
            proposal, why = parse_proposal(answer)
            if proposal is None:
                async with self.c.lock:
                    self.c.event("resolver", f"no proposal for {node_id}: {why}", node_id)
                    self.c.save()
                return
            err = await self.propose(node_id, at, proposal)
            if err is None or err == "stale":
                return
            text = (f"Your proposal for `{node_id}` was not taken: {err}. Send it again as the "
                    "fenced JSON block your instructions describe.")

    async def propose(self, node_id: str, escalation_at: str, proposal: dict,
                      run: str | None = None) -> str | None:
        """Record the proposal on the escalation raised at ``escalation_at``. Returns why it
        was not taken – None when it was; "stale" when the escalation was decided first."""
        c = self.c
        async with c.lock:
            node = c.nodes.get(node_id)
            esc = node.escalation if node else None
            if esc is None or node.status != ESCALATED or esc.at != escalation_at:
                c.event("resolver", f"proposal for {node_id} is stale: its escalation was "
                                    "decided first", node_id)
                c.save()
                return "stale"
            actions = self.actions(node_id)
            action = proposal.get("action")
            if action not in actions:
                return f"action must be one of {', '.join(actions)} (got {action!r})"
            payload = proposal.get("payload") or {}
            if not isinstance(payload, dict):
                return "payload must be a JSON object"
            atts = [a for a in proposal.get("attestations") or [] if isinstance(a, str)]
            n = len(c.docs.list(c.p(f"control/proposals/{_slug(node_id)}-"))) + 1
            p = ResolverProposal(action=action, payload=payload,
                                 rationale=str(proposal.get("rationale") or "")[:4000],
                                 attestations=atts, run=run)
            rec = c.write_record(f"control/proposals/{_slug(node_id)}-{n}.json",
                                 p.model_dump(mode="json") | {"node": node_id,
                                                              "escalation_at": esc.at})
            p.record = rec.attestation
            esc.proposal = p
            c.event("resolver", f"proposes {action} for {node_id}: {p.rationale[:200]}",
                    node_id, run)
            c.save()
            return None
