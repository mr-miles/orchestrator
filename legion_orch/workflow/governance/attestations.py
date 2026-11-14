"""Human attestations (G-AT1): evidence a human vouches for, such as an independent baseline or
a security evaluation, with coverage and waiver rules."""
from __future__ import annotations

from .. import attest, risk
from ..model import (
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


async def record_attestation(e: ChangeView, attested_by: str, payload: dict) -> dict:
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
    covers = payload.get("covers") or None
    tasks = {b.task.id for b in e.build_nodes()}
    if covers is not None:
        unknown = set(covers) - tasks
        if unknown:
            raise GovernanceError(f"G-AT1: covers unknown task(s) {sorted(unknown)}")
    warnings = []
    prev = e.change.attestations.get(kind)
    if prev and prev.verdict != verdict:
        warnings.append(f"attestation_verdict_superseded: {prev.verdict} -> {verdict}")
    if covers is not None and tasks - set(covers):
        warnings.append(f"attestation_partial_coverage: missing {sorted(tasks - set(covers))}")
    if kind == "independent-baseline" and e.change.first_build_at:
        warnings.append("attestation_after_execution: a task run already started, so "
                        "independent_baseline can no longer be satisfied for this change")
    rec = AttestationRecord(kind=kind, verdict=verdict, attested_by=attested_by, sources=sources,
                            covers=covers, waiver_reason=reason,
                            supersedes=prev.attestation if prev else None, warnings=warnings)
    rec.attestation = e.write_record(f"control/attestations/{kind}.json", rec).attestation
    e.change.attestations[kind] = rec
    return {"ok": True, "attestation": rec.attestation, "warnings": warnings}
