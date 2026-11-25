import type {
  ActionResult,
  AgentQuestion,
  AttentionItem,
  Change,
  ChangeEnd,
  ChangeStatus,
  CommandResult,
  EnvironmentBuild,
  FailureSource,
  Progress,
  ResolverProposal,
  Risk,
  Run,
  RunFailure,
  RunReport,
  RunStage,
  RunStatus,
  Severity,
  Status as TicketStatus,
  Ticket,
  Turn,
  Verification,
  VerificationRun,
  Waiting,
} from './api.gen'

export type {
  ActionResult,
  AgentQuestion,
  AttentionItem,
  Change,
  ChangeEnd,
  ChangeStatus,
  CommandResult,
  EnvironmentBuild,
  FailureSource,
  Progress,
  ResolverProposal,
  Risk,
  Run,
  RunFailure,
  RunReport,
  RunStage,
  RunStatus,
  Severity,
  Ticket,
  TicketStatus,
  Turn,
  Verification,
  VerificationRun,
  Waiting,
}

// The controller's JSON API (legion_orch/server/views.py and web.py): what the UI reads.

export interface EnvironmentProfile {
  tools: string[]
  offline?: boolean
  resources?: Record<string, string | number> | null
}

/** The change's pull request on GitHub, once published (control/pr.json). */
export interface PullRequestRecord {
  url: string
  number: number
  /** the published branch: <publish_prefix><change-id> */
  branch: string
  head: string
  labels: string[]
  workspace: string
}

/** A sign-off waiting for a decision owner: what it shows (its subjects: name -> the exact
 * documents, or ``git:<sha>`` for ``head``). */
export interface SignoffWaiting {
  subjects: Record<string, string | string[]>
  attempt: number
}

export interface ChangeState {
  project: string
  repo: string
  decision_owners: string[]
  assets_sha: string
  limits: Record<string, unknown>
  change: Change | null
  /** the tree: the root first, every ticket with its parent, subtask groups and criteria */
  tickets: Ticket[]
  runs: Run[]
  /** side processes' tickets, outside the tree (the retrospective) */
  hooks: Ticket[]
  /** the approved version of each named output */
  pointers: Record<string, string | string[]>
  /** why the workflow is paused (none: it runs) */
  paused: string[]
  /** sign-offs waiting for a decision owner, by ticket */
  signoffs: Record<string, SignoffWaiting>
  environment: EnvironmentSpec | null
  pull_request?: PullRequestRecord | null
  /** ticket -> the log of its verification running now */
  verifying?: Record<string, string>
  verifications?: VerificationRun[]
  /** agent-to-agent: the change's cards and its agents' questions to the humans */
  a2a?: { cards: A2ACard[]; questions: AgentQuestion[] }
}

/** An A2A card: the change's orchestrator or resolver, or a ticket's agent (``<role>@<ticket>``). */
export interface A2ACard {
  id: string
  ticket: string | null
  role: string | null
  title: string
  /** orchestrator · not started · running · stopped */
  state: string
}

export interface ChangeSummary {
  id: string
  workspace?: string
  goal: string
  status: ChangeStatus
  tier?: string | null
  created_at: string
  passed: number
  tickets: number
  live_runs: number
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
  /** the workflows a change can be started with */
  workflows?: string[]
  default_workflow?: string
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
  /** each tool's version – the same in every image – and where it comes from: the mise.toml
   * that runs, this change's earlier provisioning (kept), or the latest (null: looked up when
   * the environment is provisioned) */
  versions?: {
    tool: string
    version: string | null
    source: 'mise.toml' | 'kept' | 'latest'
    in_mise_toml: boolean
    provisioned: boolean
  }[]
}
