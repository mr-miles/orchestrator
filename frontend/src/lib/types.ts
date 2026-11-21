// The controller's JSON API (legion_orch/server/views.py and web.py): what the UI reads.

export type NodeStatus =
  | 'PENDING'
  | 'READY'
  | 'RUNNING'
  | 'VERIFYING'
  | 'REVIEWING'
  | 'DEFINING'
  | 'MERGING'
  | 'REWORK'
  | 'ACCEPTED'
  | 'ESCALATED'
  | 'FAILED'
  | 'AWAITING_HUMAN'

export type RunStatus = 'STARTING' | 'ACTIVE' | 'REPORTED' | 'ACCEPTED' | 'SUPERSEDED' | 'FAILED'
export type RunStage = 'PREPARING' | 'LAUNCHING' | 'RUNNING' | 'EXITED'
export type FailureSource = 'AGENT' | 'SYSTEM' | 'TIMEOUT' | 'CONTROLLER'
export type ChangeStatus = 'ACTIVE' | 'READY' | 'NOT_READY' | 'ABANDONED'

export interface Finding {
  severity: 'BLOCKER' | 'WARNING' | 'SUGGESTION'
  confidence: 'HIGH' | 'MEDIUM' | 'LOW'
  dimension?: string | null
  title: string
  observation?: string
  impact?: string
  fix?: string
  attestations?: string[]
}

export interface Feedback {
  cycle: number
  source: string
  text: string
  attestations: string[]
}

export interface ReviewResult {
  verdict: string
  sha?: string | null
  run: string
  findings: Finding[]
  checked?: string[]
  doc?: string | null
}

export interface CommandResult {
  command: string
  exit_code: number
  output?: string
  /** the repository's setup (mise run setup), run first */
  setup?: boolean
}

export interface Verification {
  sha: string
  ok: boolean
  results: CommandResult[]
  infra_error?: string | null
  attestation?: string | null
}

export interface Escalation {
  kind: string
  reason: string
  resume: string
  run?: string | null
  missing_decision?: string | null
  log_tail?: string | null
  source?: FailureSource | null
  retrying?: boolean
}

export interface TaskContract {
  id: string
  title: string
  soul: string
  depends_on?: string[]
  files_modified?: string[]
  requirements?: string[]
  verification?: string[]
  scaffold?: boolean
  [key: string]: unknown
}

export interface Node {
  id: string
  kind: string
  title: string
  depends_on: string[]
  status: NodeStatus
  cycle: number
  extra_cycles?: number
  wave?: number
  task?: TaskContract | null
  runs: string[]
  sandbox?: string | null
  base_sha?: string | null
  candidate_sha?: string | null
  accepted_sha?: string | null
  feedback: Feedback[]
  reviews: Record<string, ReviewResult>
  verification: Record<string, Verification>
  escalation?: Escalation | null
  round?: number
}

export interface Progress {
  message: string
  percent?: number | null
}

export interface RunFailure {
  source: FailureSource
  stage?: RunStage | null
  reason: string
  exit_code?: number | null
}

export interface Run {
  id: string
  node_id: string
  role: string
  soul: string
  cycle: number
  status: RunStatus
  stage?: RunStage | null
  failure?: RunFailure | null
  sandbox?: string | null
  sandbox_deleted?: boolean
  branch?: string | null
  base_sha?: string | null
  /** the commit a judge works at (checked out detached) */
  judged_sha?: string | null
  prompt_pin?: string | null
  assets_sha?: string | null
  attempts: number
  violations: string[][]
  report?: { status: string; summary: string; attestations: string[] } | null
  progress: Progress[]
}

export interface GateResult {
  status: 'satisfied' | 'unsatisfied' | 'unevaluable'
  detail: string
  human_judgement?: boolean
}

export interface Readiness {
  verdict: 'READY' | 'NOT_READY'
  integration_head: string
  integration_branch: string
  gates: Record<string, GateResult>
  note: string
  attestation?: string | null
}

export interface AttestationRecord {
  kind: string
  verdict: string
  attested_by: string
  sources: string[]
  warnings: string[]
}

export interface Override {
  kind: string
  gate: string
  approver: string
  reason: string
  evidence?: string | null
}

export interface ProtectedApproval {
  task: string
  paths: string[]
  approver: string
  at: string
  reason: string
}

export interface SurfacePin {
  pins: Record<string, string | null>
  approver: string
  reason: string
}

export interface Risk {
  tier: string
  total: number
  threshold_tier: string
  floors?: { signal: string; tier: string }[]
  scores?: Record<string, number>
}

export interface EnvironmentRole {
  profile: string
  image?: string | null
  network?: Record<string, { name: string; endpoints?: { host: string; port: number }[] }>
  cpu?: string | null
  memory?: string | null
  driver_config?: string | null
}

export interface FixRound {
  number: number
  approver: string
  comment?: string | null
  head: string
  at: string
  failure: string[]
  attestation?: string | null
}

export interface Change {
  id: string
  goal: string
  blank_repo?: boolean
  round?: number
  rounds?: FixRound[]
  status: ChangeStatus
  tier?: string | null
  base_sha: string
  integration_head?: string | null
  oracle_sha?: string | null
  risk?: Risk | null
  ship?: Readiness | null
  /** gate (or subject) -> its approval record */
  approvals?: Record<string, { approver: string; at?: string }>
  attestations?: Record<string, AttestationRecord>
  overrides?: Override[]
  protected_approvals?: ProtectedApproval[]
  surface_pins?: Record<string, SurfacePin>
  environment?: {
    roles: Record<string, EnvironmentRole>
    spec?: string
    log?: string
    attestation?: string
  } | null
}

export interface H2Subject {
  subject: string
  paths: string[]
  status: 'approved' | 'stale' | 'pending'
  approver?: string | null
  warnings?: string[]
  /** path -> the version approved (a stale subject: diff against it) */
  approved?: Record<string, number>
  /** path -> the latest version */
  latest?: Record<string, number | null>
  /** ``attestation``: R3's independent-baseline, decided with the subjects */
  kind?: 'approval' | 'attestation'
  /** attestation: what approving attests (the baseline report, the controller's run) */
  sources?: string[]
  /** attestation: the controller's results of the oracle commands at the head */
  results?: { command: string; exit_code: number }[]
  ok?: boolean | null
  /** attestation: why it cannot be attested yet */
  note?: string | null
  /** the gate that decides it: H-E1 approves the plan, H-2 the oracle */
  gate?: 'H-E1' | 'H-2'
}

/** H-3: a ship gate only a human attestation satisfies, with the evidence to cite. */
export interface GateAttestation {
  gate: string
  kinds: string[]
  status: 'attested' | 'failed' | 'pending'
  kind?: string | null
  verdict?: string | null
  attested_by?: string | null
  suggested: { label: string; att: string }[]
  /** rollback_or_forward_fix_evidence: the release planner's recommended kind, and why */
  recommended?: string | null
  rationale?: string
  /** no specific evidence behind it: may be attested not_applicable with a reason */
  waivable?: boolean
  /** what a not_applicable attestation is recorded against */
  waiver_sources?: string[]
}

export interface Surface {
  id: string
  description?: string
  commands?: string[]
}

export interface EnvironmentProfile {
  tools: string[]
  offline?: boolean
  resources?: Record<string, string | number> | null
}

export interface EnvironmentSpec {
  path: string
  version: number
  spec: {
    summary: string
    rationale: string
    profiles: Record<string, EnvironmentProfile>
    roles: Record<string, string>
    proposed?: { tools?: Record<string, unknown>; policies?: Record<string, unknown> }
    /** the repository's mise.toml as proposed: committed on approval (§5.3c) */
    mise?: { content: string } | null
    setup_network?: string[]
  }
  proposals: { tools: string[]; policies: string[] }
  network: Record<string, { policy: string; tools: string[]; hosts: string[] }[]>
  problems: string[]
}

/** A stage of the change's workflow, with the nodes shown in it (nested stages too). */
export interface Stage {
  id: string
  title: string
  depth: number
  /** how many stage levels it contains (0: none) */
  level: number
  /** the gate step that ends it (none: it is done when its stages are) */
  gate: string | null
  nodes: string[]
  status: string
}

export interface ChangeState {
  project: string
  repo: string
  decision_owners: string[]
  assets_sha: string
  change: Change | null
  nodes: Node[]
  runs: Run[]
  gates: string[]
  h2: H2Subject[]
  h3?: GateAttestation[]
  /** from the workflow core: what a human may do on each node now; waiting gates' unmet
   * preconditions */
  workflow?: {
    actions: Record<string, string[]>
    unmet: Record<string, string[]>
    stages: Stage[]
  }
  surfaces: Surface[]
  environment: EnvironmentSpec | null
  attestation_kinds: string[]
}

export interface ChangeSummary {
  id: string
  workspace?: string
  goal: string
  status: ChangeStatus
  tier?: string | null
  created_at: string
  accepted: number
  nodes: number
  live_runs: number
  retrospective?: NodeStatus | null
  needs_you?: number
}

/** What a human should look at (WORKFLOW.md §2.2): blocking items wait for a decision owner;
 * inbox items are to read. ``key`` identifies the item (the same decision raised again later is
 * a new key). */
export interface AttentionItem {
  key: string
  change: string
  goal: string
  node: string | null
  kind: 'gate' | 'escalation' | 'not_ready' | 'change_ended' | 'retrospective'
  detail: string
  title: string
  reason: string
  since: string
  blocking: boolean
}

export interface Attention {
  needs_you: AttentionItem[]
  inbox: AttentionItem[]
}

export interface Digest {
  since: string
  changes: {
    id: string
    goal: string
    status: ChangeStatus | null
    events: number
    counts: Record<string, number>
  }[]
  needs_you: AttentionItem[]
  inbox: AttentionItem[]
}

export interface Meta {
  project: string
  repo: string
  decision_owners: string[]
  assets_sha: string
  live_runs: number
  max_parallel: number
}

export interface DocMeta {
  path: string
  version: number
  sha256: string
  author: string
  role: string
  created_at: string
  schema: string
  schema_sha: string
  attestation: string
}

export interface Doc extends DocMeta {
  content: string
  history: DocMeta[]
}

export type LogKind =
  | 'sandbox'
  | 'init'
  | 'text'
  | 'prompt'
  | 'thinking'
  | 'tool_call'
  | 'tool_result'
  | 'result'
  | 'waiting'
  | 'task'
  | 'event'

export interface LogEntry {
  kind: LogKind
  title?: string
  text: string
  error: boolean
}

/** One ``log`` event of a run's log stream: the entries from index ``from`` on changed. */
export interface LogUpdate {
  run: string
  status: RunStatus
  stage?: RunStage | null
  failure?: RunFailure | null
  live: boolean
  from: number
  entries: LogEntry[]
  total: number
}

export interface ActionResult {
  status?: NodeStatus
  note?: string | null
  warnings?: string[]
  pending?: string[]
  approved?: string[]
}

/** The traceability matrix (WORKFLOW.md §2.3). */
export interface TaskTrace {
  id: string
  title: string
  requirements: string[]
  verification: string[]
  scaffold: boolean
  node?: string | null
  status: string
  cycle: number
  accepted_sha?: string | null
  check?: { ok: boolean; sha: string; attestation?: string | null } | null
  reviews: { role: string; verdict: string }[]
}

export interface CriterionTrace {
  id: string
  text: string
  tests: string[]
  commands: string[]
  result?: 'pass' | 'fail' | null
}

export interface RequirementTrace {
  id: string
  text: string
  spec?: {
    path: string
    status: 'approved' | 'stale' | 'pending'
    approved?: number | null
    latest?: number | null
  } | null
  tasks: string[]
  criteria: CriterionTrace[]
}

export interface TraceWarning {
  kind: string
  message: string
  requirement?: string | null
  task?: string | null
  criterion?: string | null
  path?: string | null
  version?: number | null
}

export interface Traceability {
  built: boolean
  requirements: RequirementTrace[]
  tasks: TaskTrace[]
  warnings: TraceWarning[]
  results?: {
    head?: string | null
    verdict?: string | null
    run?: string | null
    ok?: boolean | null
    commands: { command: string; exit_code: number }[]
    attestation?: string | null
  } | null
  summary: { requirements: number; tasks: number; criteria: number; warnings: number }
}

export interface FileStat {
  path: string
  added?: number | null
  deleted?: number | null
}

/** What H-3 accepts: the diff, the protected paths it changes, the ship gates evaluated dry. */
export interface Acceptance {
  diff: {
    base?: string | null
    head?: string | null
    files: FileStat[]
    added: number
    deleted: number
    tasks: { id: string; title: string; files: FileStat[]; added: number; deleted: number }[]
    protected: { path: string; approved_by: string[]; tasks: string[] }[]
  } | null
  readiness: Readiness | null
  error?: string | null
}

export interface DocDiff {
  path: string
  from: number
  to: number
  diff: string
}

/** A repository and its document store (a controller serves one). */
export interface Workspace {
  id: string
  name: string
  repo: string
  base_branch: string
  source?: string | null
  /** changes by status */
  changes: Record<string, number>
  documents: number
}

export interface GitRef {
  name: string
  sha: string
  base: boolean
}

export interface FileEntry {
  path: string
  size?: number | null
}

export interface GitTree {
  ref: string
  sha: string
  truncated: boolean
  files: FileEntry[]
}

/** A file for the viewer: its text, or why it is not shown. */
export interface FileView {
  path: string
  size?: number | null
  text: string | null
  reason?: string | null
}

export interface ConfigFiles {
  group: string
  root: string
  assets_sha: string
  files: FileEntry[]
}
