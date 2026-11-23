import type {
  ActionResult,
  AgentQuestion,
  AttentionItem,
  AttestationRecord,
  Change,
  ChangeStatus,
  CommandResult,
  CriterionTrace,
  Escalation,
  FailureSource,
  Feedback,
  Finding,
  FixRound,
  GateAttestation,
  GateResult,
  Node,
  NodeStatus,
  Override,
  Progress,
  Readiness,
  RequirementTrace,
  ResolverProposal,
  ReviewResult,
  Risk,
  Run,
  RunFailure,
  RunStage,
  RunStatus,
  Surface,
  SurfacePin,
  TaskContract,
  TaskTrace,
  TraceWarning,
  Traceability,
  Turn,
  Verification,
} from './api.gen'

export type {
  ActionResult,
  AgentQuestion,
  AttentionItem,
  AttestationRecord,
  Change,
  ChangeStatus,
  CommandResult,
  CriterionTrace,
  Escalation,
  FailureSource,
  Feedback,
  Finding,
  FixRound,
  GateAttestation,
  GateResult,
  Node,
  NodeStatus,
  Override,
  Progress,
  Readiness,
  RequirementTrace,
  ResolverProposal,
  ReviewResult,
  Risk,
  Run,
  RunFailure,
  RunStage,
  RunStatus,
  Surface,
  SurfacePin,
  TaskContract,
  TaskTrace,
  TraceWarning,
  Traceability,
  Turn,
  Verification,
}

// The controller's JSON API (legion_orch/server/views.py and web.py): what the UI reads.

export interface ProtectedApproval {
  task: string
  paths: string[]
  approver: string
  at: string
  reason: string
}

export interface EnvironmentRole {
  profile: string
  image?: string | null
  network?: Record<string, { name: string; endpoints?: { host: string; port: number }[] }>
  cpu?: string | null
  memory?: string | null
  driver_config?: string | null
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

export interface EnvironmentProfile {
  tools: string[]
  offline?: boolean
  resources?: Record<string, string | number> | null
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

/** The change's pull request on GitHub, once published after H-3 (control/pr.json). */
export interface PullRequestRecord {
  url: string
  number: number
  /** the published branch: <publish_prefix><change-id> */
  branch: string
  head: string
  labels: string[]
  workspace: string
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
  pull_request?: PullRequestRecord | null
  /** node -> the log of its verification running now */
  verifying?: Record<string, string>
  /** agent-to-agent: the change's cards and its agents' questions to the humans */
  a2a?: { cards: A2ACard[]; questions: AgentQuestion[] }
}

/** An A2A card: the change's orchestrator, or a step agent (``<role>@<node>``) with a sandbox. */
export interface A2ACard {
  id: string
  node: string | null
  role: string | null
  title: string
  sandbox: string | null
  stopped: boolean
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
  /** the configured workspaces' names */
  workspaces: string[]
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

/** A configured repository changes are made in, and its changes' documents. */
export interface Workspace {
  id: string
  name: string
  repo: string
  base_branch: string
  source?: string | null
  /** a finished change's branch on the source: <publish_prefix><change-id> */
  publish_prefix: string
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

/** The H-E1 card's environment spec view (server/views.environment_spec). */
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
