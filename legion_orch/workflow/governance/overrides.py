"""Gate overrides (G-OV1): add a gate to the tier's set, or waive one with replacement evidence."""
from __future__ import annotations

from .. import attest, risk
from ..model import (
    GateOverride,
)
from ..view import ChangeView
from .common import GovernanceError, resolve_or_raise

# Gates a human may never waive: the machine check or the human decision *is* the gate.
NON_WAIVABLE = {"deterministic_verification", "explicit_human_approval",
                "whole_change_acceptance_evidence"}
ALL_GATES = {g for gates in risk.GATES_BY_TIER.values() for g in gates}

async def record_override(e: ChangeView, approver: str, payload: dict) -> dict:
    """Guard G-OV1."""
    kind, gate, reason = payload.get("kind"), payload.get("gate"), payload.get("reason")
    if kind not in ("add_gate", "waive_gate"):
        raise GovernanceError("G-OV1: kind must be add_gate or waive_gate (tier changes happen at H-1)")
    if gate not in ALL_GATES:
        raise GovernanceError(f"G-OV1: unknown gate {gate!r}")
    if not risk.valid_reason(reason):
        raise GovernanceError("G-OV1: a reason of ≥24 chars and more than one word is required")
    evidence = None
    if kind == "waive_gate":
        if gate in NON_WAIVABLE:
            raise GovernanceError(f"G-OV1: {gate} cannot be waived")
        evidence = attest.normalize(payload.get("evidence") or "", e.cid)
        await resolve_or_raise(e, evidence, "G-OV1: replacement evidence")
    rec = GateOverride(kind=kind, gate=gate, reason=reason, approver=approver, evidence=evidence)
    n = len(e.change.overrides) + 1
    rec.attestation = e.write_record(f"control/overrides/{n}.json", rec).attestation
    e.change.overrides.append(rec)
    return {"ok": True, "attestation": rec.attestation}


def gate_set(e: ChangeView) -> list[str]:
    gates = list(risk.GATES_BY_TIER[e.change.tier])
    for o in e.change.overrides:
        if o.kind == "add_gate" and o.gate not in gates:
            gates.append(o.gate)
    return gates


def waiver_for(e: ChangeView, gate: str) -> GateOverride | None:
    ws = [o for o in e.change.overrides if o.kind == "waive_gate" and o.gate == gate]
    return ws[-1] if ws else None
