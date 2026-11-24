import { mock } from 'bun:test'
import { AttentionItemDefaults, ChangeDefaults, RunDefaults, TicketDefaults } from '@/lib/api.gen'
import type { Attention, ChangeState, ChangeSummary, Run, Ticket } from '@/lib/types'

/** An EventSource the test drives: ``emit`` delivers a named event to the latest connection. */
export class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  static all: FakeEventSource[] = []
  readyState = FakeEventSource.CONNECTING
  onopen: ((e: Event) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  readonly url: string
  constructor(url: string) {
    super()
    this.url = url
    FakeEventSource.all.push(this)
  }
  static last(path: string): FakeEventSource {
    const es = [...FakeEventSource.all].reverse().find((e) => e.url.includes(path))
    if (!es) throw new Error(`no EventSource for ${path}`)
    return es
  }
  open() {
    this.readyState = FakeEventSource.OPEN
    this.onopen?.(new Event('open'))
  }
  emit(name: string, data: unknown) {
    this.dispatchEvent(new MessageEvent(name, { data: JSON.stringify(data) }))
  }
  close() {
    this.readyState = FakeEventSource.CLOSED
  }
}

export interface Call {
  url: string
  body?: unknown
}

/** ``fetch`` answering from ``routes`` (path prefix -> JSON), recording every call. */
export function fakeFetch(routes: Record<string, unknown>) {
  const calls: Call[] = []
  const fn = mock(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const key = Object.keys(routes)
      .filter((k) => url.startsWith(k))
      .sort((a, b) => b.length - a.length)[0]
    if (key === undefined)
      return new Response(JSON.stringify({ error: 'not found' }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      })
    return new Response(JSON.stringify(routes[key]), {
      headers: { 'content-type': 'application/json' },
    })
  })
  globalThis.fetch = fn as unknown as typeof fetch
  return calls
}

export function install() {
  FakeEventSource.all = []
  globalThis.EventSource = FakeEventSource as unknown as typeof EventSource
}

export const ticket = (t: Partial<Ticket> & Pick<Ticket, 'id' | 'type'>): Ticket => ({
  ...TicketDefaults,
  role: 'subtask',
  goal: '',
  ...t,
})

export const run = (r: Partial<Run> & Pick<Run, 'id' | 'ticket' | 'role'>): Run => ({
  ...RunDefaults,
  soul: 'intake-analyst',
  attempt: 1,
  status: 'ACTIVE',
  stage: 'RUNNING',
  sandbox: 'lg-sb',
  ...r,
})

export const CHANGES: ChangeSummary[] = [
  {
    id: 'c1',
    goal: 'Say hello',
    status: 'ACTIVE',
    tier: null,
    created_at: '2026-01-01',
    passed: 1,
    tickets: 3,
    live_runs: 0,
  },
]

const REQS = 'file:changes/c1/tickets/intake/requirements.md@1'
const RISK = 'file:changes/c1/tickets/intake/risk.json@1'

/** A change whose intake is done: its sign-off H-1 waits for a decision owner. */
export function stateAtH1(): ChangeState {
  return {
    project: 'demo',
    repo: 'app',
    decision_owners: ['miles'],
    assets_sha: 'sha256:abc',
    limits: {},
    change: {
      ...ChangeDefaults,
      id: 'c1',
      goal: 'Say hello',
      workspace: 'app',
      status: 'ACTIVE',
      base_sha: '0123456789abcdef',
    },
    tickets: [
      ticket({
        id: 'change',
        type: 'grouping',
        role: 'root',
        parent: null,
        status: 'subtasks',
        subtasks: [['workspace'], ['requirements'], ['plan']],
      }),
      ticket({
        id: 'workspace',
        type: 'controller:sync',
        parent: 'change',
        status: 'passed',
        attempt: 1,
      }),
      ticket({
        id: 'requirements',
        type: 'grouping',
        parent: 'change',
        status: 'subtasks',
        subtasks: [['intake']],
      }),
      ticket({
        id: 'intake',
        type: 'agent:intake',
        parent: 'requirements',
        goal: 'the requirements and the risk assessment',
        status: 'checking',
        attempt: 1,
        criteria: ['H-1'],
        outputs: { requirements: REQS, risk: RISK },
        runs: ['r0001'],
      }),
      ticket({
        id: 'H-1',
        type: 'human:signoff',
        parent: 'intake',
        role: 'criterion',
        status: 'working',
        attempt: 1,
        signs: ['requirements', 'risk'],
        applies: 'tier',
        waivable: false,
      }),
      ticket({ id: 'plan', type: 'grouping', parent: 'change', subtasks: [['design']] }),
      ticket({
        id: 'design',
        type: 'agent:planner',
        parent: 'plan',
        inputs: ['requirements', 'risk'],
      }),
    ],
    runs: [
      run({ id: 'r0001', ticket: 'intake', role: 'intake', status: 'ACCEPTED', stage: 'EXITED' }),
    ],
    hooks: [],
    pointers: {},
    paused: ['signing H-1'],
    signoffs: { 'H-1': { subjects: { requirements: REQS, risk: RISK }, attempt: 1 } },
    environment: null,
  }
}

/** A build waiting on a human – its review failed with the same findings twice – with the
 * resolver's proposal: waive the review. */
export function stateWaiting(): ChangeState {
  const s = stateAtH1()
  const tickets = s.tickets
    .map((t) =>
      t.id === 'H-1'
        ? { ...t, status: 'passed' as const }
        : t.id === 'intake' || t.id === 'requirements'
          ? { ...t, status: 'passed' as const }
          : t,
    )
    .concat([
      ticket({
        id: 'T1',
        type: 'agent:implementer',
        parent: 'plan',
        goal: 'write hello',
        status: 'waiting',
        waiting: 'repeated',
        attempt: 2,
        criteria: ['review:T1'],
        findings: ['[BLOCKER] greet.txt lacks a newline'],
      }),
      ticket({
        id: 'review:T1',
        type: 'agent:reviewer',
        parent: 'T1',
        role: 'criterion',
        status: 'rejected',
        waivable: true,
        findings: ['[BLOCKER] greet.txt lacks a newline'],
      }),
    ])
  return {
    ...s,
    tickets,
    paused: [],
    signoffs: {},
    pointers: { requirements: REQS, risk: RISK },
    change: s.change && {
      ...s.change,
      tier: 'R1',
      proposals: {
        T1: {
          action: 'waive:review:T1',
          payload: { reason: 'the newline is not a requirement' },
          rationale: 'The finding asks for something no requirement says.',
          attestations: [REQS],
          run: 'r0009',
          record: null,
        },
      },
    },
  }
}

/** What the controller's attention view says at H-1 (plus an ended change in the inbox). */
export function attentionAtH1(): Attention {
  const base = {
    ...AttentionItemDefaults,
    goal: 'Say hello',
    reason: '',
    since: '2026-01-01T00:00:00.000Z',
  }
  return {
    needs_you: [
      {
        ...base,
        key: 'c1|H-1|gate|H-1|t',
        change: 'c1',
        ticket: 'H-1',
        kind: 'gate',
        detail: 'H-1',
        title: 'H-1: sign off requirements, risk',
        blocking: true,
      },
    ],
    inbox: [
      {
        ...base,
        key: 'c0|None|change_ended|READY|t',
        change: 'c0',
        ticket: null,
        kind: 'change_ended',
        detail: 'READY',
        title: 'Change is ready to ship',
        blocking: false,
      },
    ],
  }
}
