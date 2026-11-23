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
    Accepted,
    Approval,
    ApprovalState,
    AttestationVerdict,
    Oracle,
    PlanPointer,
)
from ..view import ChangeView
from .common import GovernanceError
from .surfaces import pin_surfaces

PLAN_FILES = ("spec.md", "oracle.json", "taskgraph.json")      # a plan's documents
ENV_FILE = "environment.json"


def step_doc(e: ChangeView, base: str, name: str) -> str:
    """A step agent's document in the current round's step ``base`` (``baseline``,
    ``integration``): under its node id (``baseline/report.md``, ``baseline.2/report.md``)."""
    node = e.current(base)
    return f"{node.id if node else base}/{name}"


def draft(e: ChangeView) -> str:
    """The directory of the plan being made: the current round's plan step (``plan``,
    ``plan.2``) – what H-E1 approves. Once approved it is the plan worked to (``change.plan``)."""
    node = e.current("plan")
    return node.id if node else e.change.plan_node


def plan_docs(e: ChangeView, plan: str | None = None) -> list[str]:
    """A plan's documents (default: the plan worked to), relative to the change."""
    d = plan or e.change.plan_node
    return [f"{d}/{n}" for n in PLAN_FILES]


def env_spec(e: ChangeView, plan: str | None = None) -> str:
    """A plan's environment spec (default: the plan worked to)."""
    return f"{plan or e.change.plan_node}/{ENV_FILE}"


def spec_doc(e: ChangeView, req: str, plan: str | None = None) -> str:
    """A requirement's delta spec in a plan (default: the plan worked to)."""
    return f"{plan or e.change.plan_node}/specs/{req}.md"


def pinned(appr: Approval | None, name: str) -> tuple[str | None, int | None]:
    """The document named ``name`` (``environment.json``) that an approval pinned – its
    qualified path and version, wherever its plan is."""
    for p, v in (appr.pins if appr else {}).items():
        if p.endswith("/" + name):
            return p, v
    return None, None


def h2_subjects(e: ChangeView) -> dict[str, dict[str, int | None]]:
    """Subject -> {qualified path: current latest version} of the plan being made. One subject
    per delta spec."""
    d = draft(e)

    def at(rel: str) -> dict[str, int | None]:
        return {e.p(rel): e.latest(rel)}
    subjects = {"plan": at(f"{d}/spec.md") | at(f"{d}/taskgraph.json")}
    for req in e.change.requirements:
        subjects[req] = at(spec_doc(e, req, d))
    if e.change.tier and risk.rank(e.change.tier) >= 2:       # an oracle stage
        subjects["oracle"] = at(f"{d}/oracle.json")
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
        spec = env_spec(e, draft(e))
        pins = {e.p(spec): e.latest(spec)}
        out.append(SubjectStatus(
            subject=ENVIRONMENT_SUBJECT, paths=list(pins),
            status=ApprovalState.APPROVED if appr and same_docs(e, appr.pins, pins)
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


def same_docs(e: ChangeView, approved: dict[str, int], current: dict[str, int | None]) -> bool:
    """Approved documents still say what the current ones say: the same versions of the same
    paths, or – a later round's plan, its own copies – the same content, file by file (a
    round that left a document as it was keeps its approval)."""
    if approved == current:
        return True
    prefix = e.p("")

    def by_name(pins: dict[str, int | None]) -> dict[str, tuple[str, int | None]]:
        return {p[len(prefix):].split("/", 1)[-1]: (p, v) for p, v in pins.items()}
    a, c = by_name(approved), by_name(current)
    if a.keys() != c.keys() or any(v is None for _, v in c.values()):
        return False
    return all(e.docs.read(*a[n]).sha256 == e.docs.read(*c[n]).sha256 for n in a)


def h2_status(e: ChangeView) -> list[SubjectStatus]:
    out = []
    for subj, pins in h2_subjects(e).items():
        a = e.change.approvals.get(approval_key(subj))
        fresh = a is not None and same_docs(e, a.pins, pins) and (
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
    sources = [s for s in (e.latest_att(step_doc(e, BASELINE, "report.md")), ver and ver.attestation) if s]
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
    path, version = pinned(appr, ENV_FILE)
    if path is None or e.docs.latest_version(path) != version:
        return f"G-E5: {path or env_spec(e)} changed since it was approved and provisioned"
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
        # (the record is projected into change.approvals as it is written)
        if subj == "oracle":
            orc = f"{draft(e)}/oracle.json"
            acc = Accepted(subject="oracle", doc=orc, version=e.latest(orc))
            e.write_record(acc.path, acc)        # projected: change.oracle
            await pin_surfaces(e, approver, None, reason="initial pin at oracle approval")
        done.append(subj)
    work_to(e, {p: v for s in done for p, v in available[s].items()})
    return done


def work_to(e: ChangeView, pins: dict[str, int | None]) -> None:
    """Approved: the plan being made is the plan worked to (``control/plan.json``, projected
    into ``change.plan``), with the approved version of each document added."""
    d, prefix = draft(e), e.p("")
    held = e.change.plan.approved if e.change.plan and e.change.plan.node == d else {}
    e.write_record(PLAN_RECORD, PlanPointer(
        node=d, approved=held | {p[len(prefix):]: v for p, v in pins.items() if v}))


PLAN_RECORD = "control/plan.json"


def last_h2_approval_at(e: ChangeView) -> str | None:
    ats = [e.change.approvals[approval_key(s)].at for s in h2_subjects(e)
           if approval_key(s) in e.change.approvals]
    return max(ats) if ats else None
