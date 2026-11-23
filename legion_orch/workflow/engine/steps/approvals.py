"""What approving a gate records: each gate's :class:`Approves`, a policy of its
:class:`~.phases.Approval` phase.

A human approves a gate step waiting for a decision (``AWAITING_HUMAN``). The policy records
what the approval decides – the tier, the subjects, the attestations given with it – and says
whether the gate is now approved (:class:`Approved`). If it is, the runtime answers the step's
escalation through the core, and the phase checks again (``pending``, its checks)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ... import governance, risk, setup
from ...environment import EnvironmentSpec
from ...model import H_CHANGE, H_ENV, H_REQUIREMENTS, ApprovalState, TierOverride
from ...model import Approval as ApprovalRecord
from ...results import ActionResult
from ..base.common import ENV_SPEC, WorkflowError
from ..base.core import ChangeCore

if TYPE_CHECKING:
    from .phases import ActivePhase


@dataclass(frozen=True)
class Approved:
    """What an approval did: its result for the operator, and whether the gate is approved
    (every subject it asks for approved), so its step's escalation is answered."""
    result: ActionResult
    answered: bool


class Approves(ABC):
    """What approving a gate means: the actions that approve it, what each approval records,
    and what is still to approve before the gate passes."""
    actions: ClassVar[frozenset[str]]

    @abstractmethod
    def pending(self, c: ChangeCore) -> list[str]:
        """What is still to approve (approved separately, and still fresh)."""

    @abstractmethod
    async def approve(self, active: ActivePhase, approver: str, payload: dict) -> Approved:
        """Record ``approver``'s approval; raises :class:`.WorkflowError` if it is invalid."""


@dataclass(frozen=True)
class RequirementsAccepted(Approves):
    """H-1: the requirements and the risk tier (raising it is free; lowering needs a reason).
    Fixes the change's requirements and review panel; the tier decides the rest of the
    workflow's stages."""
    actions: ClassVar = frozenset({"accept", "approve"})

    def pending(self, c: ChangeCore) -> list[str]:
        return []

    async def approve(self, active: ActivePhase, approver: str, payload: dict) -> Approved:
        c = active.c
        ch = c.change
        pins = {c.p(p): c.latest(p) for p in ("intake/requirements.md", "intake/risk.json")}
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
        ch.panel = risk.review_panel(tier, ch.risk.scores)
        rec = ApprovalRecord(gate=H_REQUIREMENTS, approver=approver, pins=pins, tier=tier)
        rec.attestation = c.write_record("control/approvals/requirements.json", rec).attestation   # projected
        return Approved(ActionResult(tier=tier, panel=ch.panel), answered=True)


@dataclass(frozen=True)
class SubjectsApproved(Approves):
    """H-E1's (the plan's subjects: the spec and task graph, each delta spec, the sandbox
    environment) or H-2's (the oracle's, and from ``baseline_from`` the independent baseline's
    attestation). Each subject is a separate decision, possibly by different approvers: the
    gate is approved once every one is approved and fresh."""
    which: str                                   # "plan" | "oracle"
    baseline_from: int = 99                      # the tier rank from which the baseline counts
    actions: ClassVar = frozenset({"approve"})

    def pending(self, c: ChangeCore) -> list[str]:
        status = {x.subject: x.status for x in governance.subject_status(c)}
        out = [s for s in self._subjects(c) if status.get(s) != ApprovalState.APPROVED]
        base = self._baseline(c)
        if base is not None and base.status != ApprovalState.APPROVED:
            out.append(governance.BASELINE_SUBJECT)
        return out

    async def approve(self, active: ActivePhase, approver: str, payload: dict) -> Approved:
        """Approve some (or "all") of the gate's subjects."""
        c = active.c
        gate_subjects = self._subjects(c)
        subjects = payload.get("subjects") or "all"
        everything = subjects == "all" or subjects == ["all"]
        wanted = gate_subjects if everything else [s for s in subjects if s in gate_subjects]
        if not everything and (unknown := [s for s in subjects if s not in gate_subjects
                                          and s != governance.BASELINE_SUBJECT]):
            raise WorkflowError(f"unknown approval subject(s) {unknown}; choose from "
                                f"{gate_subjects}")
        base = self._baseline(c)
        attest = base is not None and base.status != ApprovalState.APPROVED and (
            everything or governance.BASELINE_SUBJECT in subjects)
        if attest and (base.note or not base.sources):
            raise WorkflowError(f"independent-baseline: {base.note or 'no baseline evidence'}")
        env = governance.ENVIRONMENT_SUBJECT in wanted
        wanted = [s for s in wanted if s != governance.ENVIRONMENT_SUBJECT]
        try:
            done = await governance.approve_subjects(c, wanted, approver) if wanted else []
            if env:
                await self._approve_environment(c, approver, payload)
                done.append(governance.ENVIRONMENT_SUBJECT)
            if attest:
                # the baseline attestation is decided here with the other subjects, citing the
                # baseline report and the controller's own run of its commands (G-AT1)
                await governance.record_attestation(c, approver, {
                    "kind": governance.BASELINE_SUBJECT, "verdict": "pass",
                    "sources": base.sources})
                done.append(governance.BASELINE_SUBJECT)
        except governance.GovernanceError as e:
            raise WorkflowError(str(e)) from None
        pending = self.pending(c)
        return Approved(ActionResult(approved=done, pending=pending), answered=not pending)

    def _subjects(self, c: ChangeCore) -> list[str]:
        return (governance.plan_subjects(c) if self.which == "plan"
                else governance.oracle_subjects(c))

    def _baseline(self, c: ChangeCore) -> governance.BaselineSubject | None:
        if not c.change.tier or risk.rank(c.change.tier) < self.baseline_from:
            return None
        return governance.baseline_subject(c)

    async def _approve_environment(self, c: ChangeCore, approver: str, payload: dict) -> None:
        """The sandbox environment at its current version (with the plan, at H-E1);
        provisioning follows."""
        version = c.latest(ENV_SPEC)
        if version is None:
            raise WorkflowError(f"no {ENV_SPEC} to approve")
        note = (payload.get("feedback") or "").strip() or None
        rec = ApprovalRecord(gate=H_ENV, approver=approver, pins={c.p(ENV_SPEC): version},
                             note=note)
        rec.attestation = c.write_record("control/approvals/environment.json", rec).attestation   # projected
        await self._adopt_mise(c, EnvironmentSpec.model_validate(c.json_doc(ENV_SPEC, version)))

    async def _adopt_mise(self, c: ChangeCore, spec: EnvironmentSpec) -> None:
        """§5.3c: the approved spec's mise.toml goes onto the integration branch (a controller
        commit, before any build) and is pinned as the setup that runs (G-MS1)."""
        ch = c.change
        if spec.mise is None:
            return
        head = await c.git.commit_file(ch.id, setup.MISE_TOML, spec.mise.content,
                                       f"legion {ch.id}: mise.toml approved at H-E1")
        if head != ch.integration_head:
            c.event("environment", f"committed the approved mise.toml -> {head[:12]}", H_ENV)
        ch.integration_head = head
        ch.setup_pin = setup.pin_for(spec.mise.content, H_ENV)


@dataclass(frozen=True)
class ChangeAccepted(Approves):
    """H-3: the whole change accepted at the current integration head, with the attestations
    only a human gives (one step). The gate passes once its checks – the tier's ship gates –
    hold."""
    actions: ClassVar = frozenset({"accept", "approve"})

    def pending(self, c: ChangeCore) -> list[str]:
        return []

    async def approve(self, active: ActivePhase, approver: str, payload: dict) -> Approved:
        c = active.c
        ch = c.change
        head = await c.git.integration_head(ch.id)
        if head != ch.integration_head:
            raise WorkflowError("integration head moved; refresh")
        note = (payload.get("feedback") or "").strip() or None
        rows = {r.gate: r for r in governance.h3_attestations(c)}
        wanted = payload.get("attestations") or []
        allowed = {k for r in rows.values() if r.status != "attested" for k in r.kinds}
        bad = [a.get("kind") for a in wanted
               if not isinstance(a, dict) or a.get("kind") not in allowed]
        if bad:
            raise WorkflowError(f"H-3: attestation kind(s) {bad} not needed; choose from "
                                f"{sorted(allowed)}")
        waivable = {k for r in rows.values() if r.waivable for k in r.kinds}
        for a in wanted:
            if a.get("verdict", "pass") not in ("pass", "not_applicable") or (
                    a.get("verdict") == "not_applicable" and a["kind"] not in waivable):
                raise WorkflowError(f"H-3: {a['kind']} can only be attested pass here")
        try:
            for a in wanted:
                await governance.record_attestation(c, approver, {
                    "kind": a["kind"], "verdict": a.get("verdict", "pass"),
                    "sources": a.get("sources") or [],
                    **({"waiver_reason": a.get("waiver_reason")}
                       if a.get("verdict") == "not_applicable" else {})})
        except governance.GovernanceError as e:
            raise WorkflowError(str(e)) from None
        missing = [r.gate for r in governance.h3_attestations(c) if r.status != "attested"]
        rec = ApprovalRecord(gate=H_CHANGE, approver=approver, head=head, note=note)
        rec.attestation = c.write_record("control/approvals/change.json", rec).attestation   # projected
        return Approved(ActionResult(warnings=[f"{g} has no passing attestation: H-3 waits for it"
                                               for g in missing]), answered=True)
