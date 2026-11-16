"""H-2: per-subject approvals of the plan, each delta spec and the oracle, pinned to the
exact document versions approved (guard G-B1 checks they are still current); and the freshness of
the H-E1 environment approval behind a provisioned environment (G-E5)."""
from __future__ import annotations

from pydantic import BaseModel

from ..model import (
    H_ENV,
    Approval,
    ApprovalState,
    Oracle,
)
from ..view import ChangeView
from .common import GovernanceError
from .surfaces import pin_surfaces


def h2_subjects(e: ChangeView) -> dict[str, dict[str, int | None]]:
    """Subject -> {qualified path: current latest version}. One subject per delta spec."""
    subjects = {"plan": {e.p("plan/spec.md"): e.latest("plan/spec.md"),
                         e.p("plan/taskgraph.json"): e.latest("plan/taskgraph.json")}}
    for req in e.change.requirements:
        subjects[req] = {e.p(f"plan/specs/{req}.md"): e.latest(f"plan/specs/{req}.md")}
    subjects["oracle"] = {e.p("plan/oracle.json"): e.latest("plan/oracle.json")}
    return subjects


def approval_key(subject: str) -> str:
    return "oracle" if subject == "oracle" else f"spec:{subject}"


class SubjectStatus(BaseModel):
    subject: str
    paths: list[str]
    status: ApprovalState
    approver: str | None = None
    at: str | None = None


def h2_status(e: ChangeView) -> list[SubjectStatus]:
    out = []
    for subj, pins in h2_subjects(e).items():
        a = e.change.approvals.get(approval_key(subj))
        fresh = a is not None and a.pins == pins and (
            subj != "oracle" or a.oracle_sha == e.change.oracle_sha)
        out.append(SubjectStatus(subject=subj, paths=list(pins),
                                 status=ApprovalState.APPROVED if fresh
                                 else ApprovalState.STALE if a else ApprovalState.PENDING,
                                 approver=a.approver if a else None, at=a.at if a else None))
    return out


def stale_approvals(e: ChangeView) -> list[str]:
    """Guard G-B1: every H-2 subject approved against the current bytes."""
    return [f"G-B1: {s.subject} {s.status}" for s in h2_status(e) if s.status != ApprovalState.APPROVED]


ENV_SPEC = "plan/environment.json"


def stale_environment(e: ChangeView) -> str | None:
    """Guard G-E5: the provisioned environment's H-E1 approval still pins the latest spec."""
    appr = e.change.approvals.get(H_ENV)
    if e.change.environment is None:
        return None
    if appr is None or appr.pins != {e.p(ENV_SPEC): e.latest(ENV_SPEC)}:
        return f"G-E5: {ENV_SPEC} changed since it was approved and provisioned"
    return None


async def approve_subjects(e: ChangeView, subjects: list[str] | str, approver: str) -> list[str]:
    available = h2_subjects(e)
    if subjects == "all" or subjects == ["all"]:
        subjects = list(available)
    unknown = [s for s in subjects if s not in available]
    if unknown:
        raise GovernanceError(f"unknown approval subject(s) {unknown}; choose from {list(available)}")
    done = []
    for subj in subjects:
        pins = available[subj]
        missing = [p for p, v in pins.items() if v is None]
        if missing:
            raise GovernanceError(f"{subj}: nothing to approve, missing {missing}")
        rec = Approval(gate="H-2", subject=subj, approver=approver, pins=pins,
                       oracle_sha=e.change.oracle_sha if subj == "oracle" else None)
        rec.attestation = e.write_record(
            f"control/approvals/{approval_key(subj).replace(':', '/')}.json", rec).attestation
        e.change.approvals[approval_key(subj)] = rec
        if subj == "oracle":
            e.change.oracle = Oracle.model_validate(e.json_doc("plan/oracle.json"))
            await pin_surfaces(e, approver, None, reason="initial pin at oracle approval")
        done.append(subj)
    return done


def last_h2_approval_at(e: ChangeView) -> str | None:
    ats = [e.change.approvals[approval_key(s)].at for s in h2_subjects(e)
           if approval_key(s) in e.change.approvals]
    return max(ats) if ats else None
