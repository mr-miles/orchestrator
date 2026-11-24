import type {
  ChangeSummary,
  FailureSource,
  Run,
  RunStage,
  Ticket,
  TicketStatus,
  Waiting,
} from './types'

/** Ticket statuses, finished on the left, then work in hand, waiting on the right (progress). */
export const PROGRESS_ORDER: TicketStatus[] = [
  'passed',
  'failed',
  'waiting',
  'paused',
  'merging',
  'checking',
  'working',
  'subtasks',
  'pending',
]

/** The colour of a status (a ticket's, a run's, a change's or a run stage), a CSS variable
 * defined in index.css. */
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

/** Why a ticket waits on a human, in words. */
export const WAITING_TEXT: Record<Waiting, string> = {
  attempts: 'its attempts are used up',
  repeated: 'the same findings came back',
  escalated: 'a criterion failed',
  system: 'a system error outlasted its retries',
  budget: 'its time budget is used up',
  blocked: 'its agent is blocked on a decision',
  empty: 'it has nothing to do',
}

export const isLive = (r: Run) => r.status === 'STARTING' || r.status === 'ACTIVE'

export const humanize = (s: string) => s.toLowerCase().replaceAll('_', ' ')

/** What a ticket is doing: until its live run's agent connects, how far it got. */
export function ticketState(t: Ticket, live: Run[]): string {
  const waiting =
    live.find((r) => r.stage === 'PREPARING') ?? live.find((r) => r.stage === 'LAUNCHING')
  if (t.status === 'working' && waiting?.stage) return STAGE_TEXT[waiting.stage]
  if (t.status === 'waiting' && t.waiting) return `waiting: ${t.waiting}`
  return t.status
}

/** A ticket's kind, from its type: ``agent:planner`` -> ``planner``, a sign-off, a
 * controller's action, a grouping ticket. */
export function kindOf(t: Pick<Ticket, 'type'>): string {
  const [family, rest] = t.type.split(':')
  if (family === 'human') return 'sign-off'
  return rest ?? family ?? t.type
}

export const shortSha = (sha?: string | null) => (sha ? sha.slice(0, 10) : '—')

/** A change's status drawn as a ticket status (its colour). */
export const CHANGE_DOT: Record<ChangeSummary['status'], string> = {
  ACTIVE: 'working',
  READY: 'passed',
  ABANDONED: 'cancelled',
}

export const CHANGE_STATUSES: ChangeSummary['status'][] = ['ACTIVE', 'READY', 'ABANDONED']

/** Whether the model that served a run is the one it asked for: the same name or an alias of
 * it (``opus`` is served as ``claude-opus-…``) – as the controller judges it (runs.py). */
export function modelMatches(asked: string, served: string): boolean {
  const bare = (m: string) => m.toLowerCase().split('[')[0] ?? ''
  const [a, s] = [bare(asked), bare(served)]
  return a === s || s.split('-').includes(a) || s.startsWith(a)
}

/** A run's model, as shown: what served it (else what it asked for), its effort, and whether
 * it was served by another model than it asked for. Null: neither is known. */
export function modelOf(run: {
  model?: string | null
  effort?: string | null
  model_served?: string | null
}): { text: string; mismatch: boolean } | null {
  const shown = run.model_served ?? run.model
  if (!shown) return null
  const mismatch = Boolean(
    run.model && run.model_served && !modelMatches(run.model, run.model_served),
  )
  const effort = run.effort ? ` (${run.effort})` : ''
  return {
    text: mismatch ? `${run.model_served}, asked for ${run.model}${effort}` : `${shown}${effort}`,
    mismatch,
  }
}
