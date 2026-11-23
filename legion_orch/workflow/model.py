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
    WORKSPACE = "workspace"              # the controller syncs the change's workspace (first)
    INTAKE = "intake"
    PLAN = "plan"
    PROVISION = "provision"
    ORACLE = "oracle"
    PREFLIGHT = "preflight"              # the controller runs the oracle's commands (R2+)
    BASELINE = "baseline"
    BUILD = "build"
    INTEGRATION = "integration"
    DESCRIBE = "describe"                # the change's pull request description, judged
    PUBLISH = "publish"                  # after H-3: the controller opens the pull request
    RELEASE = "release"                  # (kept for live-system workflows; in no tier's tree)
    HUMAN = "human"
    RETROSPECTIVE = "retrospective"      # on demand, once the change has ended


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
    DEFINING = "DEFINING"                 # an accepted plan's tasks become the workflow's steps


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
    CRITIQUE_BUDGET = "CRITIQUE_BUDGET"   # grant one more plan cycle; the plan is reworked
    VERIFYING = "VERIFYING"               # re-run deterministic verification
    REVIEWING = "REVIEWING"               # re-dispatch the missing panel member(s)
    MERGING = "MERGING"                   # merge the accepted candidate again


# Short aliases used throughout the engine (members compare and serialise as their strings).
INTAKE, PLAN, ORACLE, BASELINE, BUILD, INTEGRATION, RELEASE, HUMAN = (
    NodeKind.INTAKE, NodeKind.PLAN, NodeKind.ORACLE, NodeKind.BASELINE,
    NodeKind.BUILD, NodeKind.INTEGRATION, NodeKind.RELEASE, NodeKind.HUMAN)
PROVISION, RETRO, PREFLIGHT = NodeKind.PROVISION, NodeKind.RETROSPECTIVE, NodeKind.PREFLIGHT
WORKSPACE, DESCRIBE, PUBLISH = NodeKind.WORKSPACE, NodeKind.DESCRIBE, NodeKind.PUBLISH
H_REQUIREMENTS, H_SPEC, H_CHANGE = "H-1", "H-2", "H-3"      # human gate node ids
H_ENV = "H-E1"                                              # approve the sandbox environment
PENDING, READY, RUNNING, VERIFYING, REVIEWING, MERGING, REWORK, ACCEPTED, ESCALATED, FAILED, \
    AWAITING_HUMAN, DEFINING = tuple(NodeStatus)
ACTIVE_NODE = {RUNNING, VERIFYING, REVIEWING, DEFINING, MERGING}
R_STARTING, R_ACTIVE, R_REPORTED, R_ACCEPTED, R_SUPERSEDED, R_FAILED = tuple(RunStatus)
LIVE_RUN = {R_STARTING, R_ACTIVE}

# Which agent role executes each node kind
ROLE_FOR_KIND = {INTAKE: "intake", PLAN: "planner", ORACLE: "oracle",
                 BASELINE: "baseline", BUILD: "implementer", INTEGRATION: "integration",
                 DESCRIBE: "describer", RELEASE: "release", RETRO: "retrospective"}


class Record(BaseModel):
    """Base for persisted records: unknown keys from older stores are ignored."""
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------- plan documents
class TaskContract(Record):
    """One task of the plan's ``taskgraph.json`` (shape enforced by the taskgraph schema)."""
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
    # the blank repository's scaffold (§5.3b): built before the oracle and H-2
    scaffold: bool = False


class Criterion(Record):
    id: str
    requirement: str
    text: str
    tests: list[str] = []         # acceptance_tests that check it (G-P12)
    commands: list[str] = []      # acceptance_commands that check it (G-P12)


class Surface(Record):
    id: str
    description: str = ""
    commands: list[str]
    pins: list[str]


class Oracle(Record):
    """The plan's ``oracle.json`` as accepted (shape enforced by the oracle schema)."""
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
    head: str | None = None  # decisions: the integration head the human answered at


class ReviewResult(Record):
    verdict: Verdict
    sha: str | None = None
    run: str
    findings: list[Finding] = []
    checked: list[str] = []
    doc: str | None = None           # attestation of the review document


class ReviewReport(Record):
    """A judge's report as accepted (``<step records>/reports/<role>-cycle-<n>.json``): the
    review it records on its node (``node.reviews``, projected) and the report itself."""
    run: str
    role: str
    review: ReviewResult
    status: str
    summary: str = ""
    attestations: list[str] = []
    data: dict[str, Any] = {}        # the structured result (the role's report_schema)
    at: str = Field(default_factory=now_iso)


class CommandResult(Record):
    command: str
    exit_code: int
    output: str = ""
    setup: bool = False              # the repository's setup (mise run setup), run first


class Verification(Record):
    """A deterministic verification run (G-V1)."""
    sha: str
    ok: bool = False
    results: list[CommandResult] = []
    infra_error: str | None = None
    sandbox: str | None = None
    log: str | None = None           # its log's id (the sandbox's name): /api/runs/<log>/log
    at: str = Field(default_factory=now_iso)
    attestation: str | None = None   # the stored verification record
    run: str | None = None           # the verification service's run (VerificationRun.id)
    cached: bool = False             # the result of an earlier run of the same key


class VerificationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    INFRA_ERROR = "infra_error"      # not a result: never reused, the next request runs again
    CANCELLED = "cancelled"          # superseded by a newer push before it started


V_FINAL = {VerificationState.PASSED, VerificationState.FAILED}
V_LIVE = {VerificationState.QUEUED, VerificationState.RUNNING}


class VerificationRun(Record):
    """One verification of the service (services/verification.py): ``commands`` at ``sha`` with
    the setup pin, verifier image and timeout of its key. A passed or failed run is the key's
    result for good (once per sha); requests for it later are served from it."""
    id: str                          # v-<hash of the key>[-<attempt>]
    key: str                         # the key's hash
    sha: str
    commands: list[str] = []
    setup_pin: str | None = None
    image: str | None = None
    timeout_s: int = 0
    node: str | None = None          # the step whose commands they are
    branch: str | None = None        # the run branch it was pushed to (superseding)
    state: VerificationState = VerificationState.QUEUED
    attempt: int = 1
    requested_by: list[str] = []     # "step:<node>", "push:<run>", "agent:<run>", "operator:<who>"
    priority: int = 0                # lower runs first (step, gate checks, push, agents)
    sandbox: str | None = None       # the verifier sandbox (its log id)
    result: Verification | None = None    # compact (no output); the stored record has it all
    queued_at: str = Field(default_factory=now_iso)
    started_at: str | None = None
    ended_at: str | None = None


class PanelDecision(Record):
    """A panel's pass (G-RV1): every judge that sat passed the candidate ``sha`` in ``cycle``,
    each citing its review document. What the merge checks (G-A1), with the verification."""
    sha: str
    cycle: int
    judges: list[str] = []               # the roles that sat (none: an empty panel)
    reviews: list[str] = []              # attestations of their review documents
    at: str = Field(default_factory=now_iso)
    attestation: str | None = None       # the stored decision record


class ResolverProposal(Record):
    """The resolver's proposal for an escalation: one of the node's human actions, with its
    payload, reasons and evidence. Shown on the escalation; a human applies, edits or ignores
    it – it changes nothing by itself."""
    action: str
    payload: dict[str, Any] = {}
    rationale: str = ""
    attestations: list[str] = []
    at: str = Field(default_factory=now_iso)
    run: str | None = None           # the resolver's run that answered
    record: str | None = None        # attestation of control/proposals/<node>-<n>.json


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
    proposal: ResolverProposal | None = None   # the resolver's (A2A); a human decides


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
    status_since: str | None = None       # when it last changed status (stamped on save)
    cycle: int = 1
    task: TaskContract | None = None      # build contract from the plan's taskgraph.json
    wave: int = 0
    runs: list[str] = []
    current_run: str | None = None
    # the sandbox the node's producer keeps across rework cycles (implementer; intake, planner
    # and oracle until their gate passes), and whether it is stopped between runs
    sandbox: str | None = None
    sandbox_stopped: bool = False
    base_sha: str | None = None
    candidate_sha: str | None = None
    accepted_sha: str | None = None
    feedback: list[Feedback] = []
    finding_history: list[list[str]] = []  # HIGH finding fingerprints per review cycle
    reviews: dict[str, ReviewResult] = {}  # review_key(cycle, role) or "integration"
    verification: dict[str, Verification] = {}   # str(cycle) or "integration"
    # the roles of the step's agents (producer, critic, panel), set when the step starts: each
    # has an A2A card while the step is in progress, whether its agent has started or not
    agent_roles: list[str] = []
    # a human resumed its verification (retry, answer): the next one is a fresh attempt – what
    # they fixed (the test setup, a flaky service) is outside the verification's key
    reverify: bool = False
    panel: dict[str, PanelDecision] = {}  # str(cycle): the panel's pass at that cycle
    escalation: Escalation | None = None
    extra_cycles: int = 0                 # granted by a human on escalation
    override: Override | None = None
    approved: bool = False                # a gate step: a human approved it in this attempt
    # fix rounds (§5.10a): a build task's round; for the plan node, the round being planned and
    # the cycle it started at (the round's critique budget and feedback count from there)
    round: int = 1
    round_start_cycle: int = 1
    updated_at: str = Field(default_factory=now_iso)

    @staticmethod
    def review_key(cycle: int, role: str) -> str:
        return f"{cycle}:{role}"

    def review(self, cycle: int, role: str) -> ReviewResult | None:
        return self.reviews.get(self.review_key(cycle, role))

    def current_verification(self) -> Verification | None:
        return self.verification.get(str(self.cycle))

    def current_panel(self) -> PanelDecision | None:
        return self.panel.get(str(self.cycle))

    @property
    def records(self) -> str:
        """Where the controller files this step's records (relative to the change): a build
        task's ``tasks/<task_id>``, any other step's ``control/steps/<node id>``. Each kind in
        its own directory, per cycle – ``verification/cycle-<n>.json``, ``panel/cycle-<n>.json``,
        ``reports/<role>-cycle-<n>.json`` – so the path says whose record it is."""
        return f"tasks/{self.task.id}" if self.task else f"control/steps/{self.id}"


def step_record(rel: str) -> tuple[str, str, str] | None:
    """``(node id, kind, name)`` of a step record's path (relative to the change) – the inverse
    of :attr:`Node.records`; ``None`` for any other path."""
    parts = rel.split("/")
    if len(parts) == 4 and parts[0] == "tasks":
        return f"build:{parts[1]}", parts[2], parts[3]
    if len(parts) == 5 and parts[:2] == ["control", "steps"]:
        return parts[2], parts[3], parts[4]
    return None


def cycle_of(name: str) -> int:
    """The cycle of a per-cycle record's file name (``cycle-2.json``, ``reviewer-cycle-2.json``)."""
    return int(name.rsplit("cycle-", 1)[1].removesuffix(".json"))


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


class TaskReply(Record):
    """How the agent ended (or paused) its run's task: ``a2a_reply`` on the orchestrator's task."""
    state: str                            # completed | input-required | failed | rejected
    text: str = ""
    at: str = Field(default_factory=now_iso)


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
    # the values bound into the prompt's template variables: with the asset files of assets_sha
    # (control/assets/), they re-render the exact prompt (prompt_pin checks it)
    template_vars: dict[str, str] = {}
    session_id: str | None = None         # the agent CLI's session (from its log)
    resumed_session: str | None = None    # the agent session this run continued
    resumed_from: str | None = None       # the earlier run that session belongs to
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
    # the agent's turn ended after its accepted report, and the report went to its phase
    concluded: bool = False
    exit_code: int | None = None
    sandbox_deleted: bool = False
    # its sandbox was stopped (its step's reservation released: escalated, done, sent back)
    sandbox_stopped: bool = False
    # an A2A turn of the role's agent before its work started (it answered a message): not one
    # of the node's runs, it never reports; the role's run continues its sandbox and session
    consult: bool = False
    verification_requests: int = 0        # verifications it asked for (MCP), against its limit
    # its A2A task (the orchestrator's, id = the run id): the rendered assignment is its first
    # message (the prompt pin covers it), the orchestrator's asks-again follow it, the agent's
    # reply ends it
    instructions: str = ""
    asked: list[str] = []
    reply: TaskReply | None = None
    # messages (A2A task ids) it was reminded of, ending its task with them unanswered: each once
    inbox_reminded: list[str] = []


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


class Accepted(Record):
    """``control/accepted/<subject>.json``: the version of an agent's document the controller
    accepted as the change's ``subject`` – the oracle (when it merged, as H-2 approved it), the
    risk assessment (when the intake report was accepted) – or, ``revoked``, none any more."""
    subject: str                          # "oracle" | "risk"
    doc: str | None = None                # the document, relative to the change
    version: int | None = None
    revoked: bool | None = None
    at: str = Field(default_factory=now_iso)

    @property
    def path(self) -> str:
        return f"control/accepted/{self.subject}.json"


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


class ChangeEnd(Record):
    """Who ended a change before it could ship, where and why: abandoned at an escalation, or
    rejected at H-3. The retrospective reads it (``change.json``)."""
    by: str
    node: str
    action: str                       # abandon | reject
    reason: str | None = None
    at: str = Field(default_factory=now_iso)
    attestation: str | None = None    # control/ended.json


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
    spec: str                             # attestation of the approved environment.json
    roles: dict[str, RoleEnvironment] = {}    # role (or "verifier") -> its environment
    log: str | None = None                # attestation of the build log
    at: str = Field(default_factory=now_iso)
    attestation: str | None = None        # the stored build record


class FixRound(Record):
    """A fix round (§5.10a): planned on top of the integration head after a late failure."""
    number: int                       # 2, 3, …
    approver: str
    comment: str | None = None
    head: str                         # the integration head the round starts from
    at: str = Field(default_factory=now_iso)
    failure: list[str] = []           # the integration report, verification and findings
    first_build_at: str | None = None  # the round's first build run
    attestation: str | None = None    # control/rounds/<n>.json


class SetupPin(Record):
    """The repository setup the controller trusts (§5.3c, G-MS1): ``mise.toml``'s blob."""
    path: str = "mise.toml"
    blob: str
    source: str                       # base (the base branch at change start) | H-E1


class AgentQuestion(Record):
    """A question an agent asked the human through the change's orchestrator card (A2A
    ``ask_human``). Its id is the A2A task's; answering it completes that task. Open questions
    are withdrawn when the asker can no longer receive the answer (its sandbox is gone, the
    controller restarted: the A2A task lived in memory)."""
    id: str
    node: str                          # the asking step
    role: str
    run: str | None = None
    question: str
    asked_at: str = Field(default_factory=now_iso)
    answer: str | None = None
    answered_by: str | None = None
    answered_at: str | None = None
    withdrawn: str | None = None       # why it can no longer be answered

    @property
    def open(self) -> bool:
        return self.answer is None and self.withdrawn is None


class Card(Record):
    """An A2A card (workflow/cards.py): who an agent is – fixed for the card's life, no state.
    Pushed to the controller's catalog as the workflow goes: a step's when it starts, removed
    when it ends; the change's own (orchestrator, resolver) for its whole life. Not part of the
    change's record."""
    id: str                          # "orchestrator", "resolver", or <role>@<node>
    node: str | None = None          # the step (None: the orchestrator)
    role: str | None = None
    title: str
    published_at: str = Field(default_factory=now_iso)


class PlanPointer(Record):
    """``control/plan.json``: the plan the change is working to – the plan step whose
    directory holds its documents (``plan``, a fix round's ``plan.2``) and, once H-2 approved
    them, the approved version of each (relative path -> version)."""
    node: str = "plan"
    approved: dict[str, int] = {}
    at: str = Field(default_factory=now_iso)


class Change(Record):
    id: str
    goal: str
    workspace: str                  # the configured workspace (repository) it is made in
    created_at: str = Field(default_factory=now_iso)
    status: ChangeStatus = ChangeStatus.ACTIVE
    base_sha: str | None = None
    # the base has no build tooling or code yet (§5.3b): the plan starts with a scaffold task
    blank_repo: bool = False
    tier: str | None = None
    risk: RiskAssessment | None = None
    tier_override: TierOverride | None = None
    approvals: dict[str, Approval] = {}   # "H-1", "H-E1", "H-3", "oracle", "spec:<subject>"
    plan: PlanPointer | None = None       # the plan worked to (control/plan.json)
    oracle: Oracle | None = None          # the plan's oracle.json as accepted
    oracle_sha: str | None = None
    integration_head: str | None = None
    panel: list[str] = []
    requirements: list[str] = []
    first_build_at: str | None = None
    ended: ChangeEnd | None = None                        # abandoned or rejected: by, where, why
    attestations: dict[str, AttestationRecord] = {}       # kind -> latest record
    overrides: list[GateOverride] = []
    protected_approvals: list[ProtectedPathApproval] = []
    surface_pins: dict[str, SurfacePin] = {}              # surface id -> pin record
    ship: Readiness | None = None
    environment: EnvironmentBuild | None = None           # provisioned after H-E1
    setup_pin: SetupPin | None = None                     # None: no setup task, nothing runs
    cleaned_at: str | None = None    # ended, retrospective done: run branches and images removed
    pruned_at: str | None = None     # a human removed the integration branch and evidence refs
    round: int = 1                                         # 1, or the current fix round
    rounds: list[FixRound] = []                            # fix rounds since the last restart
    questions: dict[str, AgentQuestion] = {}               # A2A ask_human, by task id
    verifications: dict[str, VerificationRun] = {}         # the verification service's runs

    @property
    def plan_node(self) -> str:
        """The plan worked to: its step's node, the directory of its documents (``plan``
        until a plan is approved)."""
        return self.plan.node if self.plan else "plan"

    def plan_doc(self, name: str) -> str:
        """A document of the plan worked to, relative to the change (``spec.md`` ->
        ``plan.2/spec.md``)."""
        return f"{self.plan_node}/{name}"

    def round_first_build_at(self) -> str | None:
        """When the current round's first build started (round 1: the change's first build);
        None while the round has built nothing. Approvals and the independent baseline must
        precede it."""
        return self.rounds[-1].first_build_at if self.rounds else self.first_build_at


def _parse_ts_safe(ts: str):
    from datetime import datetime
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
