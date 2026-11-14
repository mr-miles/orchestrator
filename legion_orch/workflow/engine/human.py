"""Human actions: gates, escalations, change-level governance, ship.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

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
    Approval,
    ApprovalState,
    AttestationVerdict,
    ChangeStatus,
    Feedback,
    Node,
    Override,
    Resume,
    TierOverride,
)
from ..results import ActionResult
from .common import WorkflowError, _slug


class HumanMixin:
    def _check_approver(self, approver: str) -> None:
        if approver not in self.cfg.decision_owners:
            raise WorkflowError(f"approver {approver!r} is not a decision owner "
                                f"({', '.join(self.cfg.decision_owners)})")

    async def human_action(self, node_id: str, action: str, approver: str,
                           payload: dict | None = None) -> ActionResult:
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
                res = ActionResult()
            else:
                raise WorkflowError(f"action {action!r} not available for {node_id} in {node.status}")
            self.event("human_action", f"{approver}: {action}", node_id)
            self._save()
            res.status = node.status
            return res

    def _feedback_doc(self, node: Node, approver: str, text: str, kind: str) -> str:
        n = len([f for f in node.feedback if f.source == kind]) + 1
        path = f"tasks/{node.task.id}/{kind}/{n}.md" if node.kind == BUILD \
            else f"control/{kind}/{_slug(node.id)}-{n}.md"
        return self.write_record(path, f"# {kind} for {node.id}\n\nBy: {approver}\n\n{text}\n").attestation

    # ------------------------------------------------------------------ human gates
    async def _human_gate(self, node: Node, action: str, approver: str, payload: dict) -> ActionResult:
        handler = self.GATE_ACTIONS.get((node.id, action))
        if handler is None:
            valid = [a for g, a in self.GATE_ACTIONS if g == node.id]
            raise WorkflowError(f"action {action!r} not valid for {node.id} (one of {valid})")
        return await getattr(self, handler)(node, approver, payload)

    # (gate, action) -> method(node, approver, payload)
    GATE_ACTIONS = {
        (H_REQUIREMENTS, "accept"): "_accept_requirements",
        (H_REQUIREMENTS, "revise"): "_revise_requirements",
        (H_SPEC, "approve"): "_approve_spec",
        (H_SPEC, "revise"): "_revise_spec",
        (H_CHANGE, "accept"): "_accept_change",
        (H_CHANGE, "reject"): "_reject_change",
    }

    def _send_back(self, target: Node, approver: str, payload: dict) -> None:
        """A human sends ``target``'s work back with feedback: a new cycle, reworked."""
        text = (payload.get("feedback") or "").strip()
        if not text:
            raise WorkflowError("feedback is required")
        att = self._feedback_doc(target, approver, text, "feedback")
        target.cycle += 1
        target.feedback.append(Feedback(cycle=target.cycle, source="feedback", text=text,
                                        attestations=[att]))
        target.status = REWORK

    async def _accept_requirements(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-1: accept requirements and the risk tier (raising it is free; lowering needs a
        reason). Fixes the change's requirements and review panel and creates the stage nodes."""
        ch = self.change
        pins = {self.p(p): self.latest(p) for p in ("intake/requirements.md", "intake/risk.json")}
        derived = ch.risk.tier
        tier = payload.get("tier") or derived
        if tier not in risk.TIERS:
            raise WorkflowError(f"unknown tier {tier}")
        if risk.rank(tier) < risk.rank(derived) and not risk.valid_reason(payload.get("reason")):
            raise WorkflowError("lowering the tier needs a reason of ≥24 chars and more than one word")
        if tier != derived:
            ch.tier_override = TierOverride(from_tier=derived, to_tier=tier,
                                            reason=payload.get("reason"), approver=approver)
        ch.tier = tier
        ch.requirements = guards.requirements_ids(self.doc("intake/requirements.md").content)
        ch.panel = risk.review_panel(tier, ch.risk.scores)
        rec = Approval(gate=H_REQUIREMENTS, approver=approver, pins=pins, tier=tier)
        rec.attestation = self.write_record("control/approvals/requirements.json", rec).attestation
        ch.approvals[H_REQUIREMENTS] = rec
        node.status = ACCEPTED
        self._create_stage_nodes()
        return ActionResult(tier=tier, panel=ch.panel)

    async def _revise_requirements(self, node: Node, approver: str, payload: dict) -> ActionResult:
        self._send_back(self.nodes[INTAKE], approver, payload)
        node.status = PENDING
        return ActionResult()

    async def _approve_spec(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-2: each delta spec, the plan and the oracle are separate decisions (possibly by
        different approvers); the gate completes once every subject is approved and fresh (and,
        at R3, the independent baseline is attested)."""
        try:
            done = await governance.approve_subjects(self, payload.get("subjects") or "all", approver)
        except governance.GovernanceError as e:
            raise WorkflowError(str(e)) from None
        pending = [x.subject for x in governance.h2_status(self) if x.status != ApprovalState.APPROVED]
        if pending:
            return ActionResult(approved=done, pending=pending)
        if risk.rank(self.change.tier) >= 3:
            b = self.change.attestations.get("independent-baseline")
            if not b or b.verdict != AttestationVerdict.PASS:
                return ActionResult(approved=done, pending=["independent-baseline"],
                                    note="R3: attest independent-baseline (pass) before build "
                                         "starts, then approve again to release the build")
        node.status = ACCEPTED
        return ActionResult(approved=done, pending=[])

    async def _revise_spec(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-2 revise: the plan is reworked; everything derived from it starts over (critique,
        oracle, baseline, build nodes, the integration branch and surface pins)."""
        ch = self.change
        self._send_back(self.nodes[PLAN], approver, payload)
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
        return ActionResult()

    async def _accept_change(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-3: accept the whole change at the current integration head."""
        ch = self.change
        head = await self.git.integration_head(ch.id)
        if head != ch.integration_head:
            raise WorkflowError("integration head moved; refresh")
        note = (payload.get("feedback") or "").strip() or None
        rec = Approval(gate=H_CHANGE, approver=approver, head=head, note=note)
        rec.attestation = self.write_record("control/approvals/change.json", rec).attestation
        ch.approvals[H_CHANGE] = rec
        node.status = ACCEPTED
        return ActionResult()

    async def _reject_change(self, node: Node, approver: str, payload: dict) -> ActionResult:
        self.change.status = ChangeStatus.ABANDONED
        node.status = FAILED
        return ActionResult()

    # ------------------------------------------------------------------ escalations
    def _resolve_escalation(self, node: Node, action: str, approver: str, payload: dict) -> ActionResult:
        if action == "abandon":
            node.status = FAILED
            self.change.status = ChangeStatus.ABANDONED
            return ActionResult()
        if action == "override_accept":
            return self._override_accept(node, approver, payload)
        if action not in ("retry", "answer"):
            raise WorkflowError(f"unknown escalation action {action!r}")
        resume = node.escalation.resume if node.escalation else Resume.READY
        if action == "answer":
            text = (payload.get("answer") or payload.get("feedback") or "").strip()
            if not text:
                raise WorkflowError("an answer is required")
            target = self.nodes[PLAN] if resume == Resume.CRITIQUE_BUDGET else node
            att = self._feedback_doc(target, approver, text, "decisions")
            target.feedback.append(Feedback(cycle=target.cycle, source="decisions",
                                            text=f"human decision: {text}", attestations=[att]))
        node.escalation = None
        self._resume(node, resume)
        return ActionResult(status=node.status)

    def _override_accept(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """Accept a build task without a full panel PASS. Deterministic verification at the
        candidate is still required (it is not waivable); the gate reports human judgement."""
        if node.kind != BUILD:
            raise WorkflowError("override is only available for build tasks")
        if not risk.valid_reason(payload.get("reason")):
            raise WorkflowError("override needs a reason of ≥24 chars and more than one word")
        ver = node.current_verification()
        if not node.candidate_sha or not ver or not ver.ok or ver.sha != node.candidate_sha:
            raise WorkflowError("override requires passing deterministic verification at the "
                                "candidate (deterministic_verification is not waivable)")
        node.override = Override(approver=approver, reason=payload["reason"], sha=node.candidate_sha)
        node.escalation = None
        node.status = MERGING
        self._spawn(self._merge_node(node.id))
        return ActionResult()

    def _resume(self, node: Node, resume: Resume) -> None:
        """Continue the workflow where the resolved escalation said it should."""
        if resume == Resume.CRITIQUE_BUDGET:          # one more critique cycle: rework the plan
            node.extra_cycles += 1
            plan = self.nodes[PLAN]
            plan.cycle += 1
            plan.feedback += node.feedback[-1:]
            plan.status = REWORK
            node.cycle += 1
            node.status = PENDING
        elif resume == Resume.REWORK_CYCLE:           # one more review cycle
            node.extra_cycles += 1
            cur = self.runs.get(node.current_run) if node.current_run else None
            if cur and cur.status == R_REPORTED:
                cur.status = R_SUPERSEDED
            node.cycle += 1
            node.candidate_sha = None
            node.status = REWORK
        elif resume == Resume.VERIFYING:
            node.status = VERIFYING
            self._spawn(self._verify_node(node.id))
        elif resume == Resume.REVIEWING:              # re-dispatch the missing panel member(s)
            node.status = REVIEWING
        elif resume == Resume.REWORK:
            node.status = REWORK
        else:
            node.status = READY

    async def change_action(self, action: str, approver: str, payload: dict | None = None) -> ActionResult:
        """Change-level governance: attest, override, approve_protected_paths, approve_surface."""
        payload = payload or {}
        async with self.lock:
            self._check_approver(approver)
            try:
                if action == "attest":
                    a = await governance.record_attestation(self, approver, payload)
                    res = ActionResult(attestation=a.attestation, warnings=a.warnings)
                elif action == "override":
                    o = await governance.record_override(self, approver, payload)
                    res = ActionResult(attestation=o.attestation)
                elif action == "approve_protected_paths":
                    p = governance.record_protected_approval(self, approver, payload)
                    res = ActionResult(attestation=p.attestation,
                                       note="applies only to runs of this task that start after now")
                elif action == "approve_surface":
                    if not risk.valid_reason(payload.get("reason")):
                        raise governance.GovernanceError("a reason of ≥24 chars is required to re-affirm")
                    res = ActionResult(surfaces=await governance.pin_surfaces(
                        self, approver, payload.get("surface"), payload["reason"]))
                else:
                    raise WorkflowError(f"unknown change action {action!r}")
            except governance.GovernanceError as e:
                raise WorkflowError(str(e)) from None
            self.event("human_action", f"{approver}: {action}")
            self._save()
            return res

    async def _ship(self) -> None:
        from ..ship import evaluate
        result = await evaluate(self)
        async with self.lock:
            result.attestation = self.write_record("control/ship/readiness.json", result).attestation
            self.change.ship = result
            self.change.status = ChangeStatus(result.verdict)
            self.nodes[SHIP].status = ACCEPTED
            self.event("ship", f"readiness verdict: {result.verdict}", SHIP)
            self._save()
