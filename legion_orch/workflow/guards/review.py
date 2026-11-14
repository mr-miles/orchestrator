"""Findings and verdicts of critique, review and integration (G-C1, G-RV2..G-RV4, G-INT1).

The report data has already passed its JSON Schema (G-R7) and been parsed into
:mod:`~legion_orch.workflow.report_data` models, so only the cross-field rules are checked here.
"""
from __future__ import annotations

from ..model import Finding, Verdict
from ..report_data import CritiqueData, ReviewData


def findings(found: list[Finding], atts_ok: set[str], gid: str = "G-RV2") -> list[str]:
    """Every finding has a title, a fix and evidence that is also attested by the report."""
    v = []
    for i, f in enumerate(found):
        if not f.title.strip() or not f.fix.strip():
            v.append(f"{gid}: finding {i} needs title and fix")
        if not f.attestations:
            v.append(f"{gid}: finding {i} needs at least one attestation")
        for r in f.attestations:
            if r not in atts_ok:
                v.append(f"{gid}: finding {i} attestation {r} must also be listed (and resolve) "
                         "in the report attestations")
    return v


def verdict(data: ReviewData, *, cycle: int, require_checked: bool) -> list[str]:
    """G-RV3: PASS has no HIGH BLOCKER/WARNING finding and FAIL has at least one; G-RV4: a
    first-cycle PASS lists what was checked."""
    v = []
    high = data.high
    if data.verdict == Verdict.PASS and high:
        v.append(f"G-RV3: PASS is not allowed with {len(high)} HIGH BLOCKER/WARNING finding(s)")
    if data.verdict == Verdict.FAIL and not high:
        v.append("G-RV3: FAIL requires at least one HIGH BLOCKER/WARNING finding")
    if require_checked and data.verdict == Verdict.PASS and cycle == 1 and not data.checked:
        v.append("G-RV4: a first-cycle PASS must list what was checked in data.checked")
    return v


def critique(data: CritiqueData, plan_atts: dict[str, int], latest: dict[str, int | None],
             atts_ok: set[str]) -> list[str]:
    """G-C1: REWORK names findings; the critique attests the latest version of every plan doc."""
    v = []
    if data.verdict == Verdict.REWORK and not data.findings:
        v.append("G-C1: REWORK requires at least one finding")
    v += findings(data.findings, atts_ok, gid="G-C1")
    for path, ver in latest.items():
        if plan_atts.get(path) != ver:
            v.append(f"G-C1: critique must attest file:{path}@{ver} (latest plan version)")
    return v
