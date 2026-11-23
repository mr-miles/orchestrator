"""Human attestations (G-AT1): evidence a human vouches for, such as an independent baseline or
a security evaluation, with coverage and waiver rules."""
from __future__ import annotations

from pydantic import BaseModel

from .. import attest, risk
from ..model import (
    INTEGRATION,
    AttestationKind,
    AttestationRecord,
    AttestationVerdict,
)
from ..view import ChangeView
from .common import GovernanceError, resolve_or_raise

ATTESTATION_KINDS = {k.value for k in AttestationKind}
VERDICTS = {v.value for v in AttestationVerdict}

def coverage_ok(rec: AttestationRecord, task_ids: set[str]) -> bool:
    return rec.covers is None or task_ids <= set(rec.covers)


async def record_attestation(e: ChangeView, attested_by: str, payload: dict) -> AttestationRecord:
    """Guard G-AT1."""
    kind, verdict = payload.get("kind"), payload.get("verdict")
    if kind not in ATTESTATION_KINDS:
        raise GovernanceError(f"G-AT1: unknown attestation kind {kind!r}; one of {sorted(ATTESTATION_KINDS)}")
    if verdict not in VERDICTS:
        raise GovernanceError(f"G-AT1: verdict must be one of {sorted(VERDICTS)}")
    if kind == AttestationKind.RELEASE_OBSERVATION and verdict == AttestationVerdict.PASS:
        raise GovernanceError("G-AT1: release-observation cannot be attested as pass – the release "
                              "plan is the evidence; only not_applicable (a waiver) is accepted")
    reason = payload.get("waiver_reason")
    if verdict == AttestationVerdict.NOT_APPLICABLE and not risk.valid_reason(reason):
        raise GovernanceError("G-AT1: not_applicable needs a waiver_reason of ≥24 chars "
                              "and more than one word")
    if verdict != AttestationVerdict.NOT_APPLICABLE and reason:
        raise GovernanceError("G-AT1: waiver_reason is only accepted with verdict not_applicable")
    sources = [attest.normalize(s, e.cid) for s in payload.get("sources") or [] if s]
    if not sources:
        raise GovernanceError("G-AT1: at least one source attestation is required (a waiver still "
                              "names the bytes it is about)")
    for s in sources:
        await resolve_or_raise(e, s, "G-AT1: source")
    covers = payload.get("covers")      # None: the whole change; otherwise the task ids covered
    tasks = {b.task.id for b in e.build_nodes()}
    if covers is not None:
        if not isinstance(covers, list) or not covers or not all(isinstance(c, str) for c in covers):
            raise GovernanceError("G-AT1: covers must list at least one task id (omit it to cover "
                                  "the whole change)")
        unknown = set(covers) - tasks
        if unknown:
            raise GovernanceError(f"G-AT1: covers unknown task(s) {sorted(unknown)}")
    warnings = []
    prev = e.change.attestations.get(kind)
    if prev and prev.verdict != verdict:
        warnings.append(f"attestation_verdict_superseded: {prev.verdict} -> {verdict}")
    if covers is not None and tasks - set(covers):
        warnings.append(f"attestation_partial_coverage: missing {sorted(tasks - set(covers))}")
    if kind == "independent-baseline" and e.change.round_first_build_at():
        warnings.append("attestation_after_execution: a task run already started, so "
                        "independent_baseline can no longer be satisfied for this change")
    rec = AttestationRecord(kind=kind, verdict=verdict, attested_by=attested_by, sources=sources,
                            covers=covers, waiver_reason=reason,
                            supersedes=prev.attestation if prev else None, warnings=warnings)
    e.write_record(f"control/attestations/{kind}.json", rec)    # projected: change.attestations
    return e.change.attestations[kind]


# Ship gates only a human attestation satisfies, asked for on the H-3 card (the baseline's is
# decided at H-2): gate -> the attestation kinds that satisfy it (any one).
H3_GATES: dict[str, list[str]] = {
    "security_or_e2e_evaluator": ["security-evaluation", "e2e-evaluation"],
    "rollback_or_forward_fix_evidence": ["rollback-evidence", "forward-fix-evidence"],
}


class SuggestedSource(BaseModel):
    label: str
    att: str


class GateAttestation(BaseModel):
    """One H-3 row: a gate that needs a human attestation, its status and the evidence to cite."""
    gate: str
    kinds: list[str]
    status: str                        # attested | failed | pending
    kind: str | None = None            # the current record's
    verdict: str | None = None
    attested_by: str | None = None
    suggested: list[SuggestedSource] = []
    # rollback_or_forward_fix_evidence: the release planner's recommendation (its kind) and why;
    # confirming it attests that kind
    recommended: str | None = None
    rationale: str = ""
    # no specific evidence behind it (no recommendation): may be attested not_applicable with a
    # reason, recorded against ``waiver_sources`` (what was waived: the integration report, else
    # the integration head)
    waivable: bool = False
    waiver_sources: list[str] = []


def _suggested(e: ChangeView, gate: str) -> list[SuggestedSource]:
    out: list[tuple[str, str | None]] = []
    if gate == "security_or_e2e_evaluator":
        for b in e.build_nodes():
            r = b.review(b.cycle, "security")
            out.append((f"{b.task.id} security review", r.doc if r else None))
        iv = e.current(INTEGRATION)
        if iv is not None:
            ver = iv.verification.get("integration")
            out.append(("integration report", e.latest_att("integration/report.md")))
            out.append(("controller's oracle run at the head", ver.attestation if ver else None))
    else:
        out += [("release/rollback.md", e.latest_att("release/rollback.md")),
                ("release/plan.md", e.latest_att("release/plan.md"))]
    return [SuggestedSource(label=label, att=att) for label, att in out if att]


def h3_attestations(e: ChangeView) -> list[GateAttestation]:
    """The change's gates (tier and added gates, not waived) that need a human attestation."""
    from .overrides import gate_set, waiver_for
    tasks = {b.task.id for b in e.build_nodes()}
    rows = []
    for gate in gate_set(e):
        kinds = H3_GATES.get(gate)
        if not kinds or waiver_for(e, gate):
            continue
        recs = [e.change.attestations[k] for k in kinds if k in e.change.attestations]
        failed = next((r for r in recs if r.verdict == AttestationVerdict.FAIL), None)
        passed = next((r for r in recs if r.verdict in (AttestationVerdict.PASS,
                                                        AttestationVerdict.NOT_APPLICABLE)
                       and coverage_ok(r, tasks)), None)
        cur = failed or passed or (recs[0] if recs else None)
        row = GateAttestation(
            gate=gate, kinds=kinds, status="failed" if failed else "attested" if passed
            else "pending", kind=cur.kind if cur else None, verdict=cur.verdict if cur else None,
            attested_by=cur.attested_by if cur else None, suggested=_suggested(e, gate))
        if gate == "rollback_or_forward_fix_evidence" and e.latest("release/rollback.md"):
            from ..guards import recovery_strategy
            strategy, row.rationale = recovery_strategy(e.doc("release/rollback.md").content)
            row.recommended = f"{strategy}-evidence" if strategy else None
        if not row.recommended:
            row.waivable = True
            head = e.change.integration_head
            row.waiver_sources = [a for a in (
                e.latest_att("integration/report.md")
                or (f"git:{e.workspace}@{head}" if head else None),) if a]
        rows.append(row)
    return rows
