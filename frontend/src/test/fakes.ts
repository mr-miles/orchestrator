import { mock } from 'bun:test'
import type { ChangeState, ChangeSummary, Node, Run } from '@/lib/types'

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

const node = (n: Partial<Node> & Pick<Node, 'id' | 'kind' | 'title'>): Node => ({
  depends_on: [],
  status: 'PENDING',
  cycle: 1,
  runs: [],
  feedback: [],
  reviews: {},
  verification: {},
  ...n,
})

export const run = (r: Partial<Run> & Pick<Run, 'id' | 'node_id' | 'role'>): Run => ({
  soul: 'intake-analyst',
  cycle: 1,
  status: 'ACTIVE',
  stage: 'RUNNING',
  attempts: 0,
  violations: [],
  progress: [],
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
    accepted: 1,
    nodes: 2,
    live_runs: 0,
  },
]

/** A change waiting at H-1, with intake's run done. */
export function stateAtH1(): ChangeState {
  return {
    project: 'demo',
    repo: 'app',
    decision_owners: ['miles'],
    assets_sha: 'sha256:abc',
    change: {
      id: 'c1',
      goal: 'Say hello',
      status: 'ACTIVE',
      base_sha: '0123456789abcdef',
      integration_head: '0123456789abcdef',
      risk: {
        tier: 'R1',
        total: 3,
        threshold_tier: 'R1',
        floors: [],
        scores: { security: 0, public_api: 1 },
      },
    },
    nodes: [
      node({
        id: 'intake',
        kind: 'intake',
        title: 'Intake: requirements & risk',
        status: 'ACCEPTED',
        runs: ['r0001'],
      }),
      node({
        id: 'H-1',
        kind: 'human',
        title: 'Accept requirements & tier',
        status: 'AWAITING_HUMAN',
        depends_on: ['intake'],
      }),
    ],
    runs: [
      run({ id: 'r0001', node_id: 'intake', role: 'intake', status: 'ACCEPTED', stage: 'EXITED' }),
    ],
    gates: [],
    h2: [],
    surfaces: [],
    environment: null,
    attestation_kinds: ['e2e-evaluation'],
  }
}
