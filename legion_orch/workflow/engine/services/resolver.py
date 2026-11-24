"""The resolver (WORKFLOW.md §5.16): every escalation goes to the change's resolver agent, which
diagnoses it and proposes one of the remedies a human may choose; a human applies, edits or
ignores it.

When a ticket starts waiting on a human (``ChangeCore`` notices it as the change saves) the
resolver is sent a message through the inboxes – why it waits, its findings, its last run, the
remedies – and its answer's proposal, a fenced JSON block, is recorded for the escalation
(``Change.proposals``, and ``control/proposals/<ticket>-<n>.json``). The message goes
through the A2A layer's inbox (``EngineHost.deliver``), as any other. A proposal naming an
action not offered is returned with the reason (:data:`TRIES`); one for an escalation decided
first is stale.
"""
from __future__ import annotations

import json
import re

from ... import cards
from ...model import ResolverProposal
from ...tickets import Remedy, Status, Ticket, allowed_remedies
from ...tickets.plan_doc import parse_plan
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


def remedies(c: ChangeCore, ticket: Ticket) -> list[str]:
    """What a human may do about a waiting ticket: its reason's remedies, and – a rejected,
    waivable criterion – waiving it."""
    out = sorted(str(r) for r in allowed_remedies(ticket) if r != Remedy.WAIVE)
    out += [f"waive:{cid}" for cid in ticket.criteria
            if (x := c.tickets.t.get(cid)) and x.status == Status.REJECTED and x.waivable]
    return out


def escalation_key(ticket: Ticket) -> str:
    """Which escalation a proposal is about: the reason and the round it waits in."""
    return f"{ticket.waiting}@{ticket.attempt}"


class Resolver:
    def __init__(self, core: ChangeCore):
        self.c = core

    def escalated(self, event):
        """The bus: a ticket escalated – asked unless the resolver is off (``resolver.enabled``)."""
        return self.resolve(event.ticket) if self.c.cfg.resolver.enabled else None

    def brief(self, ticket_id: str) -> dict | None:
        """What the resolver is told of an escalation (None: the ticket is not waiting)."""
        t = self.c.tickets.t.get(ticket_id) if self.c.st.tickets else None
        if t is None or t.status != Status.WAITING:
            return None
        last = next((self.c.runs[r] for r in reversed(t.runs) if r in self.c.runs), None)
        return {"ticket": t.id, "type": t.type, "goal": t.goal, "waiting": str(t.waiting),
                "attempt": t.attempt, "limit": t.limit, "findings": t.findings[:30],
                "key": escalation_key(t),
                "last_run": None if last is None else {
                    "id": last.id, "role": last.role, "status": last.status,
                    "failure": last.failure.model_dump(mode="json") if last.failure else None},
                "criteria": [{"id": cid, "status": x.status, "findings": x.findings[:10]}
                             for cid in t.criteria if (x := self.c.tickets.t.get(cid))],
                "actions": remedies(self.c, t)}

    async def resolve(self, ticket_id: str) -> None:
        """Ask the resolver about the escalation of ``ticket_id``; record its proposal."""
        async with self.c.lock:
            brief = self.brief(ticket_id)
        if brief is None or self.c.change.status != "ACTIVE":
            return
        key = brief["key"]
        text = (f"Ticket `{ticket_id}` is waiting on a human. Diagnose it and propose one of its "
                f"`actions`:\n\n```json\n{json.dumps(brief, indent=1)}\n```")
        for attempt in range(1, TRIES + 1):
            try:
                _, answer = await self.c.host.deliver(self.c.cid, cards.RESOLVER, cards.ORCHESTRATOR,
                                                      cards.ORCHESTRATOR, text,
                                                      f"esc-{ticket_id}-{key}-{attempt}")
            except (NoAgent, WorkflowError) as e:
                async with self.c.lock:
                    self.c.event("resolver", f"could not ask the resolver about {ticket_id}: {e}",
                                 ticket_id)
                    self.c.save()
                return
            proposal, why = parse_proposal(answer)
            if proposal is None:
                async with self.c.lock:
                    self.c.event("resolver", f"no proposal for {ticket_id}: {why}", ticket_id)
                    self.c.save()
                return
            err = await self.propose(ticket_id, key, proposal)
            if err is None or err == "stale":
                return
            text = (f"Your proposal for `{ticket_id}` was not taken: {err}. Send it again as the "
                    "fenced JSON block your instructions describe.")

    async def propose(self, ticket_id: str, key: str, proposal: dict,
                      run: str | None = None) -> str | None:
        """Record the proposal for the escalation ``key``. Returns why it was not taken – None
        when it was; "stale" when the escalation was decided first."""
        c = self.c
        async with c.lock:
            t = c.tickets.t.get(ticket_id)
            if t is None or t.status != Status.WAITING or escalation_key(t) != key:
                c.event("resolver", f"proposal for {ticket_id} is stale: its escalation was "
                                    "decided first", ticket_id)
                c.save()
                return "stale"
            actions = remedies(c, t)
            action = proposal.get("action")
            if action not in actions:
                return f"action must be one of {', '.join(actions)} (got {action!r})"
            payload = proposal.get("payload") or {}
            if not isinstance(payload, dict):
                return "payload must be a JSON object"
            if action == "replan":                 # its plan, checked as applying it would
                plan, v = parse_plan(payload.get("plan"))
                if v or not payload.get("plan"):
                    return "a replan's payload is {\"plan\": <a replan document>}: " + (
                        "; ".join(v) or "no plan")
                if ticket_id not in plan.fail and not any(a.into == ticket_id for a in plan.add):
                    return (f"the plan must fail {ticket_id} (replaced) or add to it "
                            "(repaired)")
            atts = [a for a in proposal.get("attestations") or [] if isinstance(a, str)]
            n = len(c.docs.list(c.p(f"control/proposals/{_slug(ticket_id)}-"))) + 1
            p = ResolverProposal(action=action, payload=payload,
                                 rationale=str(proposal.get("rationale") or "")[:4000],
                                 attestations=atts, run=run)
            rec = c.write_record(f"control/proposals/{_slug(ticket_id)}-{n}.json",
                                 p.model_dump(mode="json") | {"ticket": ticket_id, "key": key})
            p.record = rec.attestation
            c.change.proposals[ticket_id] = p
            c.event("resolver", f"proposes {action} for {ticket_id}: {p.rationale[:200]}",
                    ticket_id, run)
            c.save()
            return None
