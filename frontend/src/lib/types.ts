// The controller's JSON API (legion_orch/server/views.py and web.py): what the UI reads.

export type NodeStatus =
  | 'PENDING'
  | 'READY'
  | 'RUNNING'
  | 'VERIFYING'
  | 'REVIEWING'
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
  }
  proposals: { tools: string[]; policies: string[] }
  network: Record<string, { policy: string; tools: string[]; hosts: string[] }[]>
  problems: string[]
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
  surfaces: Surface[]
  environment: EnvironmentSpec | null
  attestation_kinds: string[]
}

export interface ChangeSummary {
  id: string
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
  kind: 'gate' | 'escalation' | 'change_ended' | 'retrospective'
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
