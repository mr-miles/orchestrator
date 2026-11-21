import type { ChangeSummary, FailureSource, Node, NodeStatus, Run, RunStage } from './types'

export const STATUSES: NodeStatus[] = [
  'PENDING',
  'READY',
  'RUNNING',
  'VERIFYING',
  'REVIEWING',
  'DEFINING',
  'MERGING',
  'REWORK',
  'AWAITING_HUMAN',
  'ESCALATED',
  'ACCEPTED',
  'FAILED',
]

/** Progress bar order: finished on the left, then work in hand, waiting on the right. */
export const PROGRESS_ORDER: NodeStatus[] = [
  'ACCEPTED',
  'FAILED',
  'ESCALATED',
  'AWAITING_HUMAN',
  'REWORK',
  'MERGING',
  'DEFINING',
  'REVIEWING',
  'VERIFYING',
  'RUNNING',
  'READY',
  'PENDING',
]

/** The colour of a status (or run stage), a CSS variable defined in index.css. */
export const statusColor = (s: string) => `var(--status-${s})`

export const STAGE_TEXT: Record<RunStage, string> = {
  PREPARING: 'preparing sandbox',
  LAUNCHING: 'launching agent',
  RUNNING: 'agent connected',
  EXITED: 'exited',
}

export const SOURCE_TEXT: Record<FailureSource, string> = {
  AGENT: 'agent error',
  SYSTEM: 'system error',
  TIMEOUT: 'timeout',
  CONTROLLER: 'controller error',
}

export const isLive = (r: Run) => r.status === 'STARTING' || r.status === 'ACTIVE'

export const humanize = (s: string) => s.toLowerCase().replaceAll('_', ' ')

/** What a node is doing: until its live runs' agents connect, how far they got; a plan under
 * review is being critiqued. */
export function nodeState(n: Node, live: Run[]): string {
  const waiting =
    live.find((r) => r.stage === 'PREPARING') ?? live.find((r) => r.stage === 'LAUNCHING')
  if (n.status === 'RUNNING' && waiting?.stage) return STAGE_TEXT[waiting.stage]
  if (n.kind === 'plan' && n.status === 'REVIEWING') return 'critique'
  return humanize(n.status)
}

/** A node's short label: human gates by id and title, stages by the part before the colon. */
export const nodeLabel = (n: Node) =>
  n.kind === 'human' ? `${n.id} ${n.title}` : (n.title.split(':')[0] ?? n.title)

export const shortSha = (sha?: string | null) => (sha ? sha.slice(0, 10) : '—')

/** A change's status drawn as a node status (its colour). */
export const CHANGE_DOT: Record<ChangeSummary['status'], string> = {
  ACTIVE: 'RUNNING',
  READY: 'ACCEPTED',
  NOT_READY: 'FAILED',
  ABANDONED: 'FAILED',
}

export const CHANGE_STATUSES: ChangeSummary['status'][] = [
  'ACTIVE',
  'NOT_READY',
  'READY',
  'ABANDONED',
]
