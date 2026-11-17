"""Workflow state: the change, its task graph (nodes) and runs, as typed pydantic models.

Every record is persisted as a versioned document in the store (see engine/state.py);
``model_dump(mode="json")`` is the stored form and ``model_validate`` rebuilds it on restart.
"""
from __future__ import annotations

from datetime import UTC
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..util import now_iso


# ---------------------------------------------------------------------------- enums
class NodeKind(StrEnum):
    INTAKE = "intake"
    PLAN = "plan"
    CRITIQUE = "critique"
    PROVISION = "provision"
    ORACLE = "oracle"
    BASELINE = "baseline"
    BUILD = "build"
    INTEGRATION = "integration"
    RELEASE = "release"
    SHIP = "ship"
    HUMAN = "human"


class NodeStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    MERGING = "MERGING"
    REWORK = "REWORK"
    ACCEPTED = "ACCEPTED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    AWAITING_HUMAN = "AWAITING_HUMAN"


class RunStatus(StrEnum):
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    REPORTED = "REPORTED"
    ACCEPTED = "ACCEPTED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class RunStage(StrEnum):
    """How far a run's sandbox got (independent of :class:`RunStatus`, which is the outcome)."""
    PREPARING = "PREPARING"      # OpenShell is creating the sandbox
    LAUNCHING = "LAUNCHING"      # the payload is in place; the setup script and agent are starting
    RUNNING = "RUNNING"          # the agent has connected to the controller (first MCP call)
    EXITED = "EXITED"            # the agent process has ended


class FailureSource(StrEnum):
    """Who a run's failure came from: the agent itself, or what it runs on."""
    AGENT = "AGENT"              # the agent reported or ended with an error (API error, gave up)
    SYSTEM = "SYSTEM"            # sandbox, OS or process failure (create, setup script, crash)
    TIMEOUT = "TIMEOUT"          # the run exceeded limits.run_timeout_s
    CONTROLLER = "CONTROLLER"    # the controller failed (restart, error applying a report)


class ChangeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    READY = "READY"
    NOT_READY = "NOT_READY"
    ABANDONED = "ABANDONED"


class Severity(StrEnum):
    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    SUGGESTION = "SUGGESTION"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Verdict(StrEnum):
    """A judge's verdict: reviewers and integration PASS/FAIL, the critic PASS/REWORK."""
    PASS = "PASS"
    FAIL = "FAIL"
    REWORK = "REWORK"


class ReportStatus(StrEnum):
    DONE = "DONE"
    BLOCKED = "BLOCKED"


class AttestationKind(StrEnum):
    INDEPENDENT_BASELINE = "independent-baseline"
    SECURITY_EVALUATION = "security-evaluation"
    E2E_EVALUATION = "e2e-evaluation"
    ARCHITECTURE_REVIEW = "architecture-review"
    ROLLBACK_EVIDENCE = "rollback-evidence"
    FORWARD_FIX_EVIDENCE = "forward-fix-evidence"
    RELEASE_OBSERVATION = "release-observation"


class AttestationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"      # a waiver (needs a reason)


class OverrideKind(StrEnum):
    ADD_GATE = "add_gate"
    WAIVE_GATE = "waive_gate"


class GateStatus(StrEnum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNEVALUABLE = "unevaluable"            # no evidence to evaluate yet


class ReadinessVerdict(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"


class ApprovalState(StrEnum):
    """An H-2 subject's approval against the current document versions."""
    APPROVED = "approved"
    STALE = "stale"                        # approved, but the documents changed since
    PENDING = "pending"


class Resume(StrEnum):
    """What resolving an escalation (retry/answer) resumes."""
    READY = "READY"                       # dispatch the node again
    REWORK = "REWORK"                     # dispatch a rework run in the same cycle
    REWORK_CYCLE = "REWORK_CYCLE"         # grant one more cycle and rework
    CRITIQUE_BUDGET = "CRITIQUE_BUDGET"   # grant one more critique cycle; the plan is reworked
    VERIFYING = "VERIFYING"               # re-run deterministic verification
    REVIEWING = "REVIEWING"               # re-dispatch the missing panel member(s)


# Short aliases used throughout the engine (members compare and serialise as their strings).
INTAKE, PLAN, CRITIQUE, ORACLE, BASELINE, BUILD, INTEGRATION, RELEASE, SHIP, HUMAN = (
    NodeKind.INTAKE, NodeKind.PLAN, NodeKind.CRITIQUE, NodeKind.ORACLE, NodeKind.BASELINE,
    NodeKind.BUILD, NodeKind.INTEGRATION, NodeKind.RELEASE, NodeKind.SHIP, NodeKind.HUMAN)
PROVISION = NodeKind.PROVISION
H_REQUIREMENTS, H_SPEC, H_CHANGE = "H-1", "H-2", "H-3"      # human gate node ids
H_ENV = "H-E1"                                              # approve the sandbox environment
PENDING, READY, RUNNING, VERIFYING, REVIEWING, MERGING, REWORK, ACCEPTED, ESCALATED, FAILED, \
    AWAITING_HUMAN = tuple(NodeStatus)
ACTIVE_NODE = {RUNNING, VERIFYING, REVIEWING, MERGING}
R_STARTING, R_ACTIVE, R_REPORTED, R_ACCEPTED, R_SUPERSEDED, R_FAILED = tuple(RunStatus)
LIVE_RUN = {R_STARTING, R_ACTIVE}

# Which agent role executes each node kind
ROLE_FOR_KIND = {INTAKE: "intake", PLAN: "planner", CRITIQUE: "critic", ORACLE: "oracle",
                 BASELINE: "baseline", BUILD: "implementer", INTEGRATION: "integration",
                 RELEASE: "release"}


class Record(BaseModel):
    """Base for persisted records: unknown keys from older stores are ignored."""
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------- plan documents
class TaskContract(Record):
    """One task of ``plan/taskgraph.json`` (shape enforced by the taskgraph schema)."""
    id: str
    title: str
    soul: str
    requirements: list[str]
    depends_on: list[str] = []
    files_modified: list[str]
    files_forbidden: list[str] = []
    context: list[str] = []
    instructions: str
    acceptance: list[str]
    verification: list[str]


class Criterion(Record):
    id: str
    requirement: str
    text: str


class Surface(Record):
    id: str
    description: str = ""
    commands: list[str]
    pins: list[str]


class Oracle(Record):
    """``plan/oracle.json`` as approved (shape enforced by the oracle schema)."""
    acceptance_tests: list[str] = []
    protected_support: list[str] = []
    acceptance_commands: list[str] = []
    integration_commands: list[str] = []
    criteria: list[Criterion] = []
    surfaces: list[Surface] = []


# ---------------------------------------------------------------------------- node records
class Finding(Record):
    severity: Severity
    confidence: Confidence
    dimension: str | None = None
    title: str
    observation: str = ""
    impact: str = ""
    fix: str = ""
    attestations: list[str] = []

    @property
    def high(self) -> bool:
        """A HIGH-confidence BLOCKER or WARNING: the findings that drive rework."""
        return self.confidence == Confidence.HIGH and self.severity in (Severity.BLOCKER,
                                                                        Severity.WARNING)

    @property
    def fingerprint(self) -> str:
        """Identity of a finding across review cycles (G-RV5 stale-loop detection)."""
        path = (self.attestations or [""])[0].split("@")[0]
        return f"{self.severity}|{self.title.strip().lower()}|{path}"

    def brief(self) -> Finding:
        """Compact copy for node records (the run record and the review document keep the full
        text), so repeated review cycles cannot grow a node record without bound."""
        return self.model_copy(update={"observation": self.observation[:500],
                                       "impact": self.impact[:300], "fix": self.fix[:500],
                                       "attestations": self.attestations[:5]})


class Feedback(Record):
    cycle: int
    source: str            # critique | review | verification | merge | feedback | decisions
    text: str
    attestations: list[str] = []


class ReviewResult(Record):
    verdict: Verdict
    sha: str | None = None
    run: str
    findings: list[Finding] = []
    checked: list[str] = []
    doc: str | None = None           # attestation of the review document


class CommandResult(Record):
    command: str
    exit_code: int
    output: str = ""


class Verification(Record):
    """A deterministic verification run (G-V1)."""
    sha: str
    ok: bool = False
    results: list[CommandResult] = []
    infra_error: str | None = None
    sandbox: str | None = None
    at: str = Field(default_factory=now_iso)
    attestation: str | None = None   # the stored verification record


class Escalation(Record):
    kind: str                        # blocked | run_failed | cycle_budget | verify_unrunnable | ...
    reason: str
    resume: Resume
    at: str = Field(default_factory=now_iso)
    run: str | None = None
    missing_decision: str | None = None
    log_tail: str | None = None
    source: FailureSource | None = None   # run failures: agent error or system error
    retrying: bool = False                # retry chosen: the old sandbox is being removed


class Override(Record):
    """A human accepted a build task without a full panel PASS (verification still required)."""
    approver: str
    reason: str
    sha: str
    at: str = Field(default_factory=now_iso)


class Node(Record):
    id: str
    kind: NodeKind
    title: str
    depends_on: list[str] = []
    status: NodeStatus = PENDING
    cycle: int = 1
    task: TaskContract | None = None      # build contract from plan/taskgraph.json
    wave: int = 0
    runs: list[str] = []
    current_run: str | None = None
    sandbox: str | None = None            # implementer sandbox (reused across rework cycles)
    base_sha: str | None = None
    candidate_sha: str | None = None
    accepted_sha: str | None = None
    feedback: list[Feedback] = []
    finding_history: list[list[str]] = []  # HIGH finding fingerprints per review cycle
    reviews: dict[str, ReviewResult] = {}  # review_key(cycle, role) or "integration"
    verification: dict[str, Verification] = {}   # str(cycle) or "integration"
    escalation: Escalation | None = None
    extra_cycles: int = 0                 # granted by a human on escalation
    override: Override | None = None
    updated_at: str = Field(default_factory=now_iso)

    @staticmethod
    def review_key(cycle: int, role: str) -> str:
        return f"{cycle}:{role}"

    def review(self, cycle: int, role: str) -> ReviewResult | None:
        return self.reviews.get(self.review_key(cycle, role))

    def current_verification(self) -> Verification | None:
        return self.verification.get(str(self.cycle))


# ---------------------------------------------------------------------------- run records
class RunReport(Record):
    status: ReportStatus
    summary: str
    attestations: list[str] = []
    data: dict[str, Any] = {}         # role-specific, validated against the role's report schema
    blocked_reason: str | None = None
    missing_decision: str | None = None
    at: str = Field(default_factory=now_iso)


class Progress(Record):
    message: str
    percent: float | None = None
    at: str = Field(default_factory=now_iso)


class RunFailure(Record):
    source: FailureSource
    stage: RunStage | None = None    # how far the run had got when it failed
    reason: str
    exit_code: int | None = None


class Run(Record):
    id: str
    node_id: str
    role: str
    soul: str
    cycle: int
    status: RunStatus = R_STARTING
    stage: RunStage | None = None
    failure: RunFailure | None = None
    sandbox: str | None = None
    sandbox_ref: str | None = None        # workload id in the sandbox's SPIFFE ID
    base_sha: str | None = None
    branch: str | None = None
    judged_sha: str | None = None         # candidate/head under judgement (judge roles)
    prompt_pin: str | None = None
    assets_sha: str | None = None         # sha256 of the asset set used to render the prompt
    write_scope: list[str] = []
    required_docs: list[str] = []
    context_refs: list[str] = []
    # The rendered assignment returned to the agent by get_assignment() (template variables,
    # task contract, context, feedback); an opaque bag passed through verbatim.
    assignment: dict[str, Any] = {}
    attempts: int = 0
    docs_written: int = 0                 # document versions written by this run (G-W4 quota)
    # versions this run wrote again unchanged (written earlier by another run or the controller):
    # they count as its own for G-R4
    confirmed_docs: list[str] = []
    doc_bytes: int = 0
    violations: list[list[str]] = []
    report: RunReport | None = None
    progress: list[Progress] = []
    created_at: str = Field(default_factory=now_iso)
    ended_at: str | None = None
    exit_code: int | None = None
    sandbox_deleted: bool = False


# ---------------------------------------------------------------------------- change records
class RiskFloor(Record):
    signal: str
    tier: str


class RiskAssessment(Record):
    total: int
    threshold_tier: str
    floors: list[RiskFloor] = []
    tier: str
    scores: dict[str, int]


class TierOverride(Record):
    from_tier: str
    to_tier: str
    reason: str | None = None
    approver: str
    at: str = Field(default_factory=now_iso)


class Approval(Record):
    """A human gate decision: H-1, one H-2 subject (plan, a delta spec, the oracle) or H-3."""
    gate: str
    approver: str
    at: str = Field(default_factory=now_iso)
    pins: dict[str, int] = {}         # document path -> approved version
    subject: str | None = None        # H-2 subject
    oracle_sha: str | None = None     # H-2 oracle subject
    head: str | None = None           # H-3: integration head accepted
    tier: str | None = None           # H-1: the accepted tier
    note: str | None = None
    attestation: str | None = None    # the stored approval record


class AttestationRecord(Record):
    kind: AttestationKind
    verdict: AttestationVerdict
    attested_by: str
    at: str = Field(default_factory=now_iso)
    sources: list[str]
    covers: list[str] | None = None   # task ids; None = the whole change
    waiver_reason: str | None = None
    supersedes: str | None = None
    warnings: list[str] = []
    attestation: str | None = None


class GateOverride(Record):
    kind: OverrideKind
    gate: str
    reason: str
    approver: str
    at: str = Field(default_factory=now_iso)
    evidence: str | None = None       # waive_gate: replacement evidence
    attestation: str | None = None


class ProtectedPathApproval(Record):
    task: str
    paths: list[str]
    approver: str
    reason: str
    at: str = Field(default_factory=now_iso)
    attestation: str | None = None


class SurfacePin(Record):
    surface: str
    pins: dict[str, str | None]       # pinned path -> blob sha (None: absent when pinned)
    head: str
    approver: str
    reason: str
    at: str = Field(default_factory=now_iso)
    attestation: str | None = None


class GateResult(Record):
    status: GateStatus
    detail: str
    human_judgement: bool = False
    added: bool = False


class Readiness(Record):
    verdict: ReadinessVerdict
    tier: str
    integration_head: str
    integration_branch: str
    gates: dict[str, GateResult]
    waived_gates: list[str] = []
    human_judgement_gates: list[str] = []
    note: str = "Readiness verdict only. Nothing was published, deployed or released."
    attestation: str | None = None


class RoleEnvironment(Record):
    """The provisioned sandbox environment of one role (or the verifier) (G-PV1)."""
    profile: str
    image: str | None = None              # None: the configured default image
    # network_policies entries added to the sandbox policy (key -> entry), as resolved at
    # provisioning; no credentials, so not OpenShell providers
    network: dict[str, dict] = {}
    cpu: str | None = None                # sandbox create --cpu
    memory: str | None = None             # sandbox create --memory
    driver_config: str | None = None      # --driver-config-json (disk)


class EnvironmentBuild(Record):
    """The provisioned environment: what each mapped role's sandboxes are created with."""
    spec: str                             # attestation of the approved plan/environment.json
    roles: dict[str, RoleEnvironment] = {}    # role (or "verifier") -> its environment
    log: str | None = None                # attestation of the build log
    at: str = Field(default_factory=now_iso)
    attestation: str | None = None        # the stored build record


class Change(Record):
    id: str
    goal: str
    created_at: str = Field(default_factory=now_iso)
    status: ChangeStatus = ChangeStatus.ACTIVE
    base_sha: str | None = None
    tier: str | None = None
    risk: RiskAssessment | None = None
    tier_override: TierOverride | None = None
    approvals: dict[str, Approval] = {}   # "H-1", "H-E1", "H-3", "oracle", "spec:<subject>"
    oracle: Oracle | None = None          # plan/oracle.json as approved
    oracle_sha: str | None = None
    integration_head: str | None = None
    panel: list[str] = []
    requirements: list[str] = []
    first_build_at: str | None = None
    attestations: dict[str, AttestationRecord] = {}       # kind -> latest record
    overrides: list[GateOverride] = []
    protected_approvals: list[ProtectedPathApproval] = []
    surface_pins: dict[str, SurfacePin] = {}              # surface id -> pin record
    ship: Readiness | None = None
    environment: EnvironmentBuild | None = None           # provisioned after H-E1


def _parse_ts_safe(ts: str):
    from datetime import datetime
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
