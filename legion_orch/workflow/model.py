"""Workflow state: the change, its task graph (nodes) and runs, as typed pydantic models.

Every record is persisted as a versioned document in the store (see engine/state.py);
``model_dump(mode="json")`` is the stored form and ``model_validate`` rebuilds it on restart.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..util import now_iso

# ---------------------------------------------------------------------------- enums


class FailureSource(StrEnum):
    """Who a run's failure came from: the agent itself, or what it runs on."""
    AGENT = "AGENT"              # the agent reported or ended with an error (API error, gave up)
    SYSTEM = "SYSTEM"            # sandbox, OS or process failure (create, setup script, crash)
    TIMEOUT = "TIMEOUT"          # the run exceeded limits.run_timeout_s
    CONTROLLER = "CONTROLLER"    # the controller failed (restart, error applying a report)


class ChangeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    READY = "READY"
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


class RunStatus(StrEnum):
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    REPORTED = "REPORTED"
    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"


class RunStage(StrEnum):
    """How far a run's sandbox got (independent of :class:`RunStatus`, which is the outcome)."""
    PREPARING = "PREPARING"      # OpenShell is creating the sandbox
    LAUNCHING = "LAUNCHING"      # the payload is in place; the setup script and agent are starting
    RUNNING = "RUNNING"          # the agent has connected to the controller (first MCP call)
    EXITED = "EXITED"            # the agent process has ended


# Short aliases used throughout the engine (members compare and serialise as their strings).
R_STARTING, R_ACTIVE, R_REPORTED, R_ACCEPTED, R_FAILED = tuple(RunStatus)
LIVE_RUN = {R_STARTING, R_ACTIVE}

class Record(BaseModel):
    """Base for persisted records: unknown keys from older stores are ignored."""
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------- plan documents


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


# ---------------------------------------------------------------------------- run records
class RunReport(Record):
    status: ReportStatus
    summary: str
    attestations: list[str] = []
    data: dict[str, Any] = {}         # role-specific, validated against the role's report schema
    outputs: dict[str, Any] = {}      # the role's outputs: name -> its document(s), pinned
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


class Turn(Record):
    """One turn of a run's agent: its process, from start to exit. A turn that ends with the
    run's task open is followed by another (the orchestrator asks again, ``asked``)."""
    n: int
    started_at: str = Field(default_factory=now_iso)
    ended_at: str | None = None
    exit_code: int | None = None
    asked: str | None = None              # the orchestrator's message it started on (turn 2+)


class Run(Record):
    id: str
    ticket: str                           # the ticket whose work it is
    role: str
    soul: str
    attempt: int                          # the ticket's attempt it is a run of
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
    outputs: list[str] = []               # the output names its report must name (its role's)
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
    turns: list[Turn] = []                # its agent's turns, in order (the agent runner's)


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


class ChangeEnd(Record):
    """Who ended a change before it could ship, where and why: abandoned at an escalation, or
    rejected at H-3. The retrospective reads it (``change.json``)."""
    by: str
    node: str
    action: str                       # abandon | reject
    reason: str | None = None
    at: str = Field(default_factory=now_iso)
    attestation: str | None = None    # control/ended.json


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
    ticket: str                        # the asking agent's ticket
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
    Pushed to the controller's catalog as the workflow goes: an agent ticket's while it is
    open, the change's own (orchestrator, resolver) for its whole life. Not part of the change's
    record."""
    id: str                          # "orchestrator", "resolver", or <role>@<ticket>
    ticket: str | None = None        # its ticket (None: the orchestrator)
    role: str | None = None
    title: str


class Change(Record):
    """A change: its goal and where it is made; how it stands. Its workflow is its tickets (the
    root's pointers: the approved outputs); this is what the tickets do not say."""
    id: str
    goal: str
    workspace: str                  # the configured workspace (repository) it is made in
    created_at: str = Field(default_factory=now_iso)
    status: ChangeStatus = ChangeStatus.ACTIVE
    base_sha: str | None = None
    # the base has no build tooling or code yet (§5.3d): the plan starts with a scaffold task
    blank_repo: bool = False
    tier: str | None = None                               # from the approved risk (H-1)
    risk: RiskAssessment | None = None
    ended: ChangeEnd | None = None                        # abandoned or rejected: by, where, why
    environment: EnvironmentBuild | None = None           # provisioned after H-E1
    setup_pin: SetupPin | None = None                     # None: no setup task, nothing runs
    completed: bool = False          # ended, and its on-complete process (retrospective) done
    cleaned_at: str | None = None    # ended, retrospective done: branches and images removed
    pruned_at: str | None = None     # a human removed the integration branch and evidence refs
    questions: dict[str, AgentQuestion] = {}               # A2A ask_human, by task id
    verifications: dict[str, VerificationRun] = {}         # the verification service's runs
    proposals: dict[str, ResolverProposal] = {}            # the resolver's, per waiting ticket
    unsynced: str | None = None      # why its workspace could not be synced (it waits: retry)
