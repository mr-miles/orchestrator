"""Human actions: gates, escalations, change-level governance, ship.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

from ...util import now_iso
from .. import governance, guards, risk
from ..model import (
    ACCEPTED,
    AWAITING_HUMAN,
    BASELINE,
    BUILD,
    CRITIQUE,
    ESCALATED,
    FAILED,
    H_CHANGE,
    H_REQUIREMENTS,
    H_SPEC,
    HUMAN,
    INTAKE,
    MERGING,
    ORACLE,
    PENDING,
    PLAN,
    R_REPORTED,
    R_SUPERSEDED,
    READY,
    REVIEWING,
    REWORK,
    RUNNING,
    SHIP,
    VERIFYING,
    Node,
)
from .common import WorkflowError, _slug


class HumanMixin:
    def _check_approver(self, approver: str) -> None:
        if approver not in self.cfg.decision_owners:
            raise WorkflowError(f"approver {approver!r} is not a decision owner "
                                f"({', '.join(self.cfg.decision_owners)})")

    async def human_action(self, node_id: str, action: str, approver: str,
                           payload: dict | None = None) -> dict:
        payload = payload or {}
        async with self.lock:
            self._check_approver(approver)
            node = self.nodes.get(node_id)
            if node is None:
                raise WorkflowError(f"unknown node {node_id}")
            if node.status == ESCALATED:
                res = self._resolve_escalation(node, action, approver, payload)
            elif node.kind == HUMAN and node.status == AWAITING_HUMAN:
                res = await self._human_gate(node, action, approver, payload)
            elif node.kind == SHIP and action == "recheck" and node.status == ACCEPTED:
                node.status = RUNNING
                self._spawn(self._ship())
                res = {"ok": True}
            else:
                raise WorkflowError(f"action {action!r} not available for {node_id} in {node.status}")
            self.event("human_action", f"{approver}: {action}", node_id, data=payload)
            self._save()
            return res

    def _feedback_doc(self, node: Node, approver: str, text: str, kind: str) -> str:
        n = len([f for f in node.feedback if f["source"] == kind]) + 1
        path = f"tasks/{node.task['id']}/{kind}/{n}.md" if node.kind == BUILD \
            else f"control/{kind}/{_slug(node.id)}-{n}.md"
        return self.write_record(path, f"# {kind} for {node.id}\n\nBy: {approver}\n\n{text}\n").attestation

    async def _human_gate(self, node: Node, action: str, approver: str, payload: dict) -> dict:
        ch = self.change
        text = (payload.get("feedback") or "").strip()
        if node.id == H_REQUIREMENTS:
            intake = self.nodes[INTAKE]
            if action == "accept":
                pins = {self.p(p): self.latest(p) for p in ("intake/requirements.md", "intake/risk.json")}
                derived = ch.risk["tier"]
                tier = payload.get("tier") or derived
                if tier not in risk.TIERS:
                    raise WorkflowError(f"unknown tier {tier}")
                if risk.rank(tier) < risk.rank(derived):
                    if not risk.valid_reason(payload.get("reason")):
                        raise WorkflowError("lowering the tier needs a reason of ≥24 chars "
                                            "and more than one word")
                if tier != derived:
                    ch.tier_override = {"from": derived, "to": tier, "reason": payload.get("reason"),
                                        "approver": approver, "at": now_iso()}
                ch.tier = tier
                ch.requirements = guards.requirements_ids(self.doc("intake/requirements.md").content)
                ch.panel = risk.review_panel(tier, ch.risk["scores"])
                att = self.write_record("control/approvals/requirements.json", {
                    "gate": "H-1", "approver": approver, "at": now_iso(), "pins": pins, "tier": tier,
                    "derived_tier": derived, "override": ch.tier_override}).attestation
                ch.approvals[H_REQUIREMENTS] = {"approver": approver, "at": now_iso(), "pins": pins,
                                                "attestation": att}
                node.status = ACCEPTED
                node.human = ch.approvals[H_REQUIREMENTS]
                self._create_stage_nodes()
                return {"ok": True, "tier": tier, "panel": ch.panel}
            if action == "revise":
                if not text:
                    raise WorkflowError("feedback is required")
                att = self._feedback_doc(intake, approver, text, "feedback")
                intake.cycle += 1
                intake.feedback.append({"cycle": intake.cycle, "source": "feedback", "text": text,
                                        "attestations": [att]})
                intake.status = REWORK
                node.status = PENDING
                return {"ok": True}
        elif node.id == H_SPEC:
            if action == "approve":
                # Each delta spec, the plan and the oracle are separate decisions (may be
                # different approvers); H-2 completes once every subject is approved and fresh.
                try:
                    done = await governance.approve_subjects(
                        self, payload.get("subjects") or "all", approver)
                except governance.GovernanceError as e:
                    raise WorkflowError(str(e)) from None
                status = governance.h2_status(self)
                pending = [x["subject"] for x in status if x["status"] != "approved"]
                if not pending and risk.rank(ch.tier) >= 3:
                    b = ch.attestations.get("independent-baseline")
                    if not b or b["verdict"] != "pass":
                        return {"ok": True, "approved": done, "pending": ["independent-baseline"],
                                "note": "R3: attest independent-baseline (pass) before build starts, "
                                        "then approve again to release the build"}
                if pending:
                    return {"ok": True, "approved": done, "pending": pending}
                node.status = ACCEPTED
                node.human = {"approved_at": governance.last_h2_approval_at(self), "subjects": status}
                return {"ok": True, "approved": done, "pending": []}
            if action == "revise":
                if not text:
                    raise WorkflowError("feedback is required")
                plan = self.nodes[PLAN]
                att = self._feedback_doc(plan, approver, text, "feedback")
                plan.cycle += 1
                plan.feedback.append({"cycle": plan.cycle, "source": "feedback", "text": text,
                                      "attestations": [att]})
                plan.status = REWORK
                for nid in (CRITIQUE, ORACLE, BASELINE):
                    if nid in self.nodes:
                        n = self.nodes[nid]
                        n.status, n.cycle = PENDING, n.cycle + 1
                for b in self.build_nodes():
                    del self.nodes[b.id]
                await self.git.init_integration(ch.id, ch.base_sha)
                ch.integration_head, ch.oracle_sha, ch.oracle = ch.base_sha, None, None
                ch.surface_pins = {}
                node.status = PENDING
                return {"ok": True}
        elif node.id == H_CHANGE:
            head = await self.git.integration_head(ch.id)
            if action == "accept":
                if head != ch.integration_head:
                    raise WorkflowError("integration head moved; refresh")
                att = self.write_record("control/approvals/change.json", {
                    "gate": "H-3", "approver": approver, "at": now_iso(), "head": head,
                    "note": text}).attestation
                ch.approvals[H_CHANGE] = {"approver": approver, "at": now_iso(), "head": head,
                                          "attestation": att}
                node.status = ACCEPTED
                node.human = ch.approvals[H_CHANGE]
                return {"ok": True}
            if action == "reject":
                ch.status = "ABANDONED"
                node.status = FAILED
                return {"ok": True}
        raise WorkflowError(f"action {action!r} not valid for {node.id}")

    def _resolve_escalation(self, node: Node, action: str, approver: str, payload: dict) -> dict:
        esc = node.escalation or {}
        text = (payload.get("answer") or payload.get("feedback") or "").strip()
        if action == "abandon":
            node.status = FAILED
            self.change.status = "ABANDONED"
            return {"ok": True}
        if action == "override_accept":
            if node.kind != BUILD:
                raise WorkflowError("override is only available for build tasks")
            if not risk.valid_reason(payload.get("reason")):
                raise WorkflowError("override needs a reason of ≥24 chars and more than one word")
            ver = node.verification.get(str(node.cycle))
            if not node.candidate_sha or not ver or not ver["ok"] or ver["sha"] != node.candidate_sha:
                raise WorkflowError("override requires passing deterministic verification at the "
                                    "candidate (deterministic_verification is not waivable)")
            node.human = {"override": True, "approver": approver, "reason": payload["reason"],
                          "at": now_iso(), "sha": node.candidate_sha}
            node.escalation = None
            node.status = MERGING
            self._spawn(self._merge_node(node.id))
            return {"ok": True}
        if action not in ("retry", "answer"):
            raise WorkflowError(f"unknown escalation action {action!r}")
        if action == "answer":
            if not text:
                raise WorkflowError("an answer is required")
            target = self.nodes[PLAN] if esc.get("resume") == "critique_budget" else node
            att = self._feedback_doc(target, approver, text, "decisions")
            target.feedback.append({"cycle": target.cycle, "source": "decisions",
                                    "text": f"human decision: {text}", "attestations": [att]})
        resume = esc.get("resume", READY)
        node.escalation = None
        if resume == "critique_budget":
            node.extra_cycles += 1
            plan = self.nodes[PLAN]
            plan.cycle += 1
            plan.feedback += node.feedback[-1:]
            plan.status = REWORK
            node.cycle += 1
            node.status = PENDING
        elif resume == "rework_cycle":
            node.extra_cycles += 1
            cur = self.runs.get(node.current_run) if node.current_run else None
            if cur and cur.status == R_REPORTED:
                cur.status = R_SUPERSEDED
            node.cycle += 1
            node.candidate_sha = None
            node.status = REWORK
        elif resume == VERIFYING:
            node.status = VERIFYING
            self._spawn(self._verify_node(node.id))
        elif resume == REVIEWING:
            node.status = REVIEWING
        else:
            node.status = resume if resume in (READY, REWORK) else READY
        return {"ok": True, "status": node.status}

    async def change_action(self, action: str, approver: str, payload: dict | None = None) -> dict:
        """Change-level governance: attest, override, approve_protected_paths, approve_surface."""
        payload = payload or {}
        async with self.lock:
            self._check_approver(approver)
            try:
                if action == "attest":
                    res = await governance.record_attestation(self, approver, payload)
                elif action == "override":
                    res = await governance.record_override(self, approver, payload)
                elif action == "approve_protected_paths":
                    res = governance.record_protected_approval(self, approver, payload)
                elif action == "approve_surface":
                    if not risk.valid_reason(payload.get("reason")):
                        raise governance.GovernanceError("a reason of ≥24 chars is required to re-affirm")
                    res = {"ok": True, "surfaces": await governance.pin_surfaces(
                        self, approver, payload.get("surface"), payload["reason"])}
                else:
                    raise WorkflowError(f"unknown change action {action!r}")
            except governance.GovernanceError as e:
                raise WorkflowError(str(e)) from None
            self.event("human_action", f"{approver}: {action}", data=payload)
            self._save()
            return res

    async def _ship(self) -> None:
        from ..ship import evaluate
        result = await evaluate(self)
        async with self.lock:
            att = self.write_record("control/ship/readiness.json", result).attestation
            result["attestation"] = att
            self.change.ship = result
            self.change.status = "READY" if result["verdict"] == "READY" else "NOT_READY"
            node = self.nodes[SHIP]
            node.status = ACCEPTED
            node.human = {"verdict": result["verdict"]}
            self.event("ship", f"readiness verdict: {result['verdict']}", SHIP, data=result["gates"])
            self._save()
