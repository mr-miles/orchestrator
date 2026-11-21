"""Per-subject approvals, pinned to the exact document versions approved (guard G-B1 checks they
are still current): at H-E1 "approve the plan" the spec and task graph, each delta spec and the
sandbox environment; at H-2 "approve the oracle" the oracle (from R2, where there is one). And the
freshness of the environment approval behind a provisioned environment (G-E5)."""
from __future__ import annotations

from pydantic import BaseModel

from .. import risk
from ..model import (
    ACCEPTED,
    BASELINE,
    H_ENV,
    Approval,
    ApprovalState,
    AttestationVerdict,
    Oracle,
)
from ..view import ChangeView
from .common import GovernanceError
from .surfaces import pin_surfaces

ENV_SPEC = "plan/environment.json"


def h2_subjects(e: ChangeView) -> dict[str, dict[str, int | None]]:
    """Subject -> {qualified path: current latest version}. One subject per delta spec."""
    subjects = {"plan": {e.p("plan/spec.md"): e.latest("plan/spec.md"),
                         e.p("plan/taskgraph.json"): e.latest("plan/taskgraph.json")}}
    for req in e.change.requirements:
        subjects[req] = {e.p(f"plan/specs/{req}.md"): e.latest(f"plan/specs/{req}.md")}
    if e.change.tier and risk.rank(e.change.tier) >= 2:       # an oracle stage
        subjects["oracle"] = {e.p("plan/oracle.json"): e.latest("plan/oracle.json")}
    return subjects


ENVIRONMENT_SUBJECT = "environment"


def plan_subjects(e: ChangeView) -> list[str]:
    """What H-E1 approves: the plan, each delta spec, and the environment (if any)."""
    return [s for s in h2_subjects(e) if s != "oracle"] + (
        [ENVIRONMENT_SUBJECT] if e.cfg.environment is not None else [])


def oracle_subjects(e: ChangeView) -> list[str]:
    """What H-2 approves: the oracle (the R3 baseline attestation is its precondition)."""
    return [s for s in h2_subjects(e) if s == "oracle"]


def subject_status(e: ChangeView) -> list[SubjectStatus]:
    """Every approval subject's status, the environment's included."""
    out = h2_status(e)
    if e.cfg.environment is not None:
        appr = e.change.approvals.get(H_ENV)
        pins = {e.p(ENV_SPEC): e.latest(ENV_SPEC)}
        out.append(SubjectStatus(
            subject=ENVIRONMENT_SUBJECT, paths=list(pins),
            status=ApprovalState.APPROVED if appr and appr.pins == pins
            else ApprovalState.STALE if appr else ApprovalState.PENDING,
            approver=appr.approver if appr else None, at=appr.at if appr else None,
            approved=dict(appr.pins) if appr else {}, latest=pins))
    return out


def approval_key(subject: str) -> str:
    return "oracle" if subject == "oracle" else f"spec:{subject}"


class SubjectStatus(BaseModel):
    subject: str
    paths: list[str]
    status: ApprovalState
    approver: str | None = None
    at: str | None = None
    warnings: list[str] = []      # for the human to weigh before approving (G-BL2)
    approved: dict[str, int] = {}         # path -> version approved (stale: what changed since)
    latest: dict[str, int | None] = {}    # path -> latest version


def h2_status(e: ChangeView) -> list[SubjectStatus]:
    out = []
    for subj, pins in h2_subjects(e).items():
        a = e.change.approvals.get(approval_key(subj))
        fresh = a is not None and a.pins == pins and (
            subj != "oracle" or a.oracle_sha == e.change.oracle_sha)
        out.append(SubjectStatus(subject=subj, paths=list(pins),
                                 status=ApprovalState.APPROVED if fresh
                                 else ApprovalState.STALE if a else ApprovalState.PENDING,
                                 approver=a.approver if a else None, at=a.at if a else None,
                                 warnings=baseline_warnings(e) if subj == "oracle" else [],
                                 approved=dict(a.pins) if a else {}, latest=pins))
    return out


def baseline_warnings(e: ChangeView) -> list[str]:
    """G-BL2 (R3): what the baseline – the oracle commands the controller ran at the head before
    any build – says about the oracle. An acceptance command that already passes cannot show
    that the change works; a result that differs from the baseline verifier's is suspect."""
    node = e.current(BASELINE)
    ver = node.verification.get("baseline") if node and node.status == "ACCEPTED" else None
    if ver is None:
        return []
    orc = e.change.oracle or Oracle()
    got = {r.command: r.exit_code for r in ver.results}
    out = [f"G-BL2: acceptance command `{c}` already passes at the baseline (before any build): "
           "it may not test the change" for c in orc.acceptance_commands if got.get(c) == 0]
    run = e.runs.get(node.current_run) if node.current_run else None
    data = run.report.data if run and run.report else {}
    for r in data.get("results", []):
        c, rc = r.get("command"), r.get("exit_code")
        if c in got and rc != got[c]:
            out.append(f"G-BL2: the baseline verifier reported `{c}` exit {rc}; "
                       f"the controller got exit {got[c]}")
    return out


BASELINE_SUBJECT = "independent-baseline"


class BaselineSubject(BaseModel):
    """R3's ``independent-baseline`` attestation, shown and decided with the H-2 subjects: the
    baseline's evidence (the agent's report and the controller's own run of the commands)."""
    subject: str = BASELINE_SUBJECT
    kind: str = "attestation"
    status: ApprovalState
    approver: str | None = None
    at: str | None = None
    verdict: str | None = None                 # the current attestation's, if any
    sources: list[str] = []                    # what approving attests (and links to)
    results: list[dict] = []                   # the controller's results at the head
    ok: bool | None = None
    note: str | None = None                    # why it cannot be attested yet


def baseline_subject(e: ChangeView) -> BaselineSubject | None:
    """At R3 (once there is a baseline node): the attestation's status. ``approved`` while a pass
    newer than the baseline verification stands; ``stale`` if the baseline ran again since;
    ``pending`` otherwise."""
    node = e.current(BASELINE)
    if not e.change.tier or risk.rank(e.change.tier) < 3 or node is None:
        return None
    ver = node.verification.get("baseline") if node.status == ACCEPTED else None
    rec = e.change.attestations.get(BASELINE_SUBJECT)
    sources = [s for s in (e.latest_att("baseline/report.md"), ver and ver.attestation) if s]
    if rec and rec.verdict == AttestationVerdict.PASS and (not ver or rec.at > ver.at):
        status = ApprovalState.APPROVED
    else:
        status = ApprovalState.STALE if rec else ApprovalState.PENDING
    return BaselineSubject(
        status=status, approver=rec.attested_by if rec else None, at=rec.at if rec else None,
        verdict=rec.verdict if rec else None, sources=rec.sources if status == "approved" and rec
        else sources, ok=ver.ok if ver else None,
        results=[{"command": r.command, "exit_code": r.exit_code} for r in ver.results] if ver
        else [], note=None if ver else "the baseline has not finished yet")


def stale_approvals(e: ChangeView) -> list[str]:
    """Guard G-B1: every H-2 subject approved against the current bytes."""
    return [f"G-B1: {s.subject} {s.status}" for s in h2_status(e) if s.status != ApprovalState.APPROVED]


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
