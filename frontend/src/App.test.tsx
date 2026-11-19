import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '@/components/theme-provider'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SettingsProvider } from '@/lib/settings'
import App from './App'
import {
  attentionAtH1,
  CHANGES,
  FakeEventSource,
  fakeFetch,
  install,
  run,
  stateAtH1,
  stateAtH2,
  traceAtH2,
  type Call,
} from './test/fakes'

function renderApp() {
  return render(
    <ThemeProvider>
      <SettingsProvider>
        <TooltipProvider>
          <App />
        </TooltipProvider>
      </SettingsProvider>
    </ThemeProvider>,
  )
}

/** Connect the change stream and deliver ``state`` (and the change list). */
async function push(state = stateAtH1()) {
  const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
  act(() => {
    es.open()
    es.emit('changes', CHANGES)
    es.emit('state', state)
  })
  return es
}

let calls: Call[]

beforeEach(() => {
  install()
  localStorage.clear()
  sessionStorage.clear()
  location.hash = ''
  calls = fakeFetch({
    '/api/meta': {
      project: 'demo',
      repo: 'app',
      decision_owners: ['miles'],
      assets_sha: 'sha256:abc',
      live_runs: 0,
      max_parallel: 4,
    },
    '/api/changes/c1/nodes/H-1/action': { status: 'ACCEPTED' },
    '/api/changes': CHANGES,
  })
})

afterEach(cleanup)

describe('the operator UI', () => {
  test('it follows the change over its event stream, not by polling', async () => {
    renderApp()
    const es = await push()
    expect(
      await screen.findByText('Say hello', { selector: '[data-slot=card-description]' }),
    ).toBeTruthy()
    expect(screen.getByRole('tab', { name: /Overview/ })).toBeTruthy()
    expect(screen.getByText('Live')).toBeTruthy()
    const next = stateAtH1()
    next.nodes[1] = { ...next.nodes[1]!, status: 'ACCEPTED' }
    act(() => es.emit('state', next))
    await waitFor(() => expect(screen.queryByRole('tab', { name: /Overview.*1/ })).toBeNull())
    expect(calls.filter((c) => c.url.includes('/state'))).toEqual([]) // never fetched
  })

  test('a waiting gate opens in a sheet and records the decision as the approver', async () => {
    localStorage.setItem('approver', 'miles')
    sessionStorage.setItem('optoken', 'tok')
    renderApp()
    await push()
    await userEvent.click(await screen.findByRole('button', { name: /H-1 · Accept requirements/ }))
    const sheet = await screen.findByRole('dialog')
    expect(
      within(sheet).getByText('Accept requirements', { selector: '[data-slot=card-title]' }),
    ).toBeTruthy()
    await userEvent.click(within(sheet).getByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/nodes/H-1/action'))).toBe(true))
    const call = calls.find((c) => c.url.endsWith('/nodes/H-1/action'))
    expect(call?.body).toEqual({
      action: 'accept',
      approver: 'miles',
      payload: { tier: null, reason: '' },
    })
  })

  test('the agents tab lists live runs and opens their log stream', async () => {
    renderApp()
    const state = stateAtH1()
    state.runs.push(
      run({
        id: 'r0002',
        node_id: 'H-1',
        role: 'planner',
        progress: [{ message: 'drafting the spec', percent: 40 }],
      }),
    )
    await push(state)
    await userEvent.click(screen.getByRole('tab', { name: /Agents/ }))
    expect(await screen.findByText('drafting the spec')).toBeTruthy()
    await userEvent.click(screen.getAllByRole('button', { name: /Log/ })[0]!)
    const log = await waitFor(() => FakeEventSource.last('/api/runs/r0002/log/events'))
    act(() =>
      log.emit('log', {
        run: 'r0002',
        status: 'ACTIVE',
        stage: 'RUNNING',
        live: true,
        from: 0,
        total: 1,
        entries: [{ kind: 'tool_call', title: 'Bash', text: 'pytest -q', error: false }],
      }),
    )
    expect(await screen.findByText('pytest -q')).toBeTruthy()
    act(() => log.emit('end', { run: 'r0002' }))
    expect(log.readyState).toBe(FakeEventSource.CLOSED)
  })

  test('the bell shows what needs you across changes and opens it', async () => {
    renderApp()
    const es = await push()
    act(() => es.emit('attention', attentionAtH1()))
    const bell = await screen.findByRole('button', { name: 'Needs you: 1; inbox: 1 unread' })
    expect(document.title).toBe('(1) Legion Orchestrator')
    await userEvent.click(bell)
    const menu = await screen.findByRole('menu')
    expect(within(menu).getByText('Change is ready to ship')).toBeTruthy()
    await userEvent.click(within(menu).getByText('H-1: Accept requirements & tier'))
    const sheet = await screen.findByRole('dialog')
    expect(
      within(sheet).getByText('Accept requirements', { selector: '[data-slot=card-title]' }),
    ).toBeTruthy()
    act(() => es.emit('attention', { needs_you: [], inbox: [] }))
    await waitFor(() => expect(document.title).toBe('Legion Orchestrator'))
  })

  test('reading an inbox item marks it read in this browser', async () => {
    renderApp()
    const es = await push()
    act(() => es.emit('attention', attentionAtH1()))
    await userEvent.click(await screen.findByRole('button', { name: /inbox: 1 unread/ }))
    await userEvent.click(within(await screen.findByRole('menu')).getByText('Mark all read'))
    await screen.findByRole('button', { name: 'Needs you: 1; inbox: 0 unread' })
    expect(JSON.parse(localStorage.getItem('inbox-read') ?? '[]')).toEqual([
      'c0|None|change_ended|READY|t',
    ])
  })

  test('a link with a node opens its sheet once', async () => {
    location.hash = '#c1/overview/H-1'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    await act(async () => {
      es.open()
      es.emit('state', stateAtH1()) // the sheet mounts open: let its effects settle in act
    })
    const sheet = await screen.findByRole('dialog')
    expect(
      await within(sheet).findByText('Accept requirements', { selector: '[data-slot=card-title]' }),
    ).toBeTruthy()
    expect(location.hash).toBe('#c1/overview')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  test('after a long absence it shows what happened meanwhile', async () => {
    localStorage.setItem('last-seen', new Date(Date.now() - 3 * 3600_000).toISOString())
    calls = fakeFetch({
      '/api/meta': {},
      '/api/changes': CHANGES,
      '/api/digest': {
        since: 'x',
        changes: [
          {
            id: 'c1',
            goal: 'Say hello',
            status: 'ACTIVE',
            events: 9,
            counts: { 'runs started': 2 },
          },
        ],
        needs_you: attentionAtH1().needs_you,
        inbox: [],
      },
    })
    renderApp()
    await push()
    expect(await screen.findByText('While you were away')).toBeTruthy()
    expect(screen.getByText(/2 runs started/)).toBeTruthy()
    expect(calls.some((c) => c.url.startsWith('/api/digest?since='))).toBe(true)
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByText('While you were away')).toBeNull())
  })

  test('H-2 shows the traceability warnings first and the diff of a stale spec', async () => {
    calls = fakeFetch({
      '/api/meta': {},
      '/api/changes': CHANGES,
      '/api/changes/c1/trace': traceAtH2(),
      '/api/doc/diff': {
        path: 'p',
        from: 1,
        to: 2,
        diff: '@@ -1 +1 @@\n-add it\n+add it, politely\n',
      },
      '/api/doc': {
        path: 'changes/c1/plan/specs/REQ-1.md',
        version: 2,
        content: '# REQ-1',
        author: 'a',
        role: 'planner',
        created_at: 't',
        schema: 'markdown',
        sha256: 'sha256:0123456789abcdef0123',
        history: [],
      },
    })
    location.hash = '#c1/overview/H-2'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    await act(async () => {
      es.open()
      es.emit('state', stateAtH2())
    })
    const sheet = await screen.findByRole('dialog')
    expect(
      await within(sheet).findByText('1 requirement · 1 task · 1 criterion · 2 warnings'),
    ).toBeTruthy()
    const warnings = within(sheet).getByRole('list', { name: 'Traceability warnings' })
    expect(within(warnings).getAllByRole('listitem')[0]?.textContent).toContain('changed since')
    await userEvent.click(
      within(sheet).getByRole('button', { name: 'changed since v1 was approved' }),
    )
    expect(await screen.findByText('+add it, politely')).toBeTruthy()
    expect(calls.some((c) => c.url.includes('/api/doc/diff?') && c.url.includes('&from=1'))).toBe(
      true,
    )
  })

  test('at R3 the baseline attestation is one of the H-2 decisions, in a wide dialog', async () => {
    calls = fakeFetch({
      '/api/meta': {},
      '/api/changes': CHANGES,
      '/api/changes/c1/trace': traceAtH2(),
      '/api/changes/c1/nodes/H-2/action': { pending: [], approved: ['independent-baseline'] },
    })
    localStorage.setItem('approver', 'miles')
    const state = stateAtH2()
    state.h2.push({
      subject: 'independent-baseline',
      kind: 'attestation',
      paths: [],
      status: 'pending',
      sources: ['file:changes/c1/baseline/report.md@1'],
      results: [{ command: 'sh tests/acceptance/test_greet.sh', exit_code: 1 }],
      ok: false,
    })
    location.hash = '#c1/overview/H-2'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    await act(async () => {
      es.open()
      es.emit('state', state)
    })
    const dialog = await screen.findByRole('dialog')
    expect(dialog.getAttribute('data-slot')).toBe('dialog-content') // not the side sheet
    expect(within(dialog).getByText('independent-baseline attestation')).toBeTruthy()
    expect(within(dialog).getByText(/0 of\s+1 already pass/)).toBeTruthy()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Attest pass' }))
    await waitFor(() =>
      expect(calls.find((c) => c.url === '/api/changes/c1/nodes/H-2/action')?.body).toMatchObject({
        action: 'approve',
        payload: { subjects: ['independent-baseline'] },
      }),
    )
  })

  test('H-3 attests the gates only a human can satisfy with the acceptance', async () => {
    calls = fakeFetch({
      '/api/meta': {},
      '/api/changes': CHANGES,
      '/api/changes/c1/trace': traceAtH2(),
      '/api/changes/c1/acceptance': { diff: null, readiness: null, error: null },
      '/api/changes/c1/nodes/H-3/action': { warnings: [] },
    })
    localStorage.setItem('approver', 'miles')
    const s = stateAtH2()
    const state = {
      ...s,
      nodes: [
        ...s.nodes.filter((n) => n.id !== 'H-2'),
        {
          ...s.nodes[0]!,
          id: 'H-3',
          kind: 'human' as const,
          title: 'Accept whole change',
          status: 'AWAITING_HUMAN' as const,
        },
      ],
      h3: [
        {
          gate: 'security_or_e2e_evaluator',
          kinds: ['security-evaluation', 'e2e-evaluation'],
          status: 'pending' as const,
          suggested: [
            {
              label: 'T1 security review',
              att: 'file:changes/c1/tasks/T1/reviews/security-cycle-1.md@1',
            },
            { label: 'integration report', att: 'file:changes/c1/integration/report.md@1' },
          ],
        },
        {
          gate: 'rollback_or_forward_fix_evidence',
          kinds: ['rollback-evidence', 'forward-fix-evidence'],
          status: 'attested' as const,
          kind: 'rollback-evidence',
          verdict: 'pass',
          attested_by: 'miles',
          suggested: [],
        },
      ],
    }
    location.hash = '#c1/overview/H-3'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    await act(async () => {
      es.open()
      es.emit('state', state)
    })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('rollback-evidence pass · miles')).toBeTruthy()
    await userEvent.click(within(dialog).getByRole('checkbox', { name: 'integration report' }))
    await userEvent.click(within(dialog).getByRole('button', { name: 'Attest & accept change' }))
    await waitFor(() =>
      expect(calls.find((c) => c.url === '/api/changes/c1/nodes/H-3/action')?.body).toMatchObject({
        action: 'accept',
        payload: {
          attestations: [
            {
              kind: 'security-evaluation',
              sources: ['file:changes/c1/tasks/T1/reviews/security-cycle-1.md@1'],
            },
          ],
        },
      }),
    )
  })

  test('a not-ready change attests and re-runs the ship check from the ship node', async () => {
    calls = fakeFetch({
      '/api/meta': {},
      '/api/changes': CHANGES,
      '/api/changes/c1/action': { attestation: 'file:x@1' },
      '/api/changes/c1/nodes/ship/action': { status: 'RUNNING' },
    })
    localStorage.setItem('approver', 'miles')
    const s = stateAtH1()
    const state = {
      ...s,
      change: s.change && {
        ...s.change,
        tier: 'R3',
        status: 'NOT_READY' as const,
        ship: {
          verdict: 'NOT_READY' as const,
          integration_head: '0123456789abcdef',
          integration_branch: 'legion/c1/integration',
          note: '',
          gates: {
            explicit_human_approval: { status: 'satisfied' as const, detail: 'H-3 by miles' },
            rollback_or_forward_fix_evidence: {
              status: 'unevaluable' as const,
              detail: 'no rollback-evidence or forward-fix-evidence attestation',
            },
          },
        },
      },
      nodes: [
        ...s.nodes,
        {
          ...s.nodes[0]!,
          id: 'ship',
          kind: 'ship' as const,
          title: 'Ship readiness check',
          status: 'FAILED' as const,
        },
      ],
      h3: [
        {
          gate: 'rollback_or_forward_fix_evidence',
          kinds: ['rollback-evidence', 'forward-fix-evidence'],
          status: 'pending' as const,
          suggested: [
            { label: 'release/rollback.md', att: 'file:changes/c1/release/rollback.md@1' },
          ],
        },
      ],
    }
    location.hash = '#c1/overview/ship'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    await act(async () => {
      es.open()
      es.emit('state', state)
    })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Not ready to ship')).toBeTruthy()
    expect(within(dialog).getByText(/1 of 2 gates not satisfied/)).toBeTruthy()
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Attest & re-run readiness check' }),
    )
    await waitFor(() =>
      expect(calls.some((c) => c.url === '/api/changes/c1/nodes/ship/action')).toBe(true),
    )
    expect(calls.find((c) => c.url === '/api/changes/c1/action')?.body).toMatchObject({
      action: 'attest',
      payload: {
        kind: 'rollback-evidence',
        verdict: 'pass',
        sources: ['file:changes/c1/release/rollback.md@1'],
      },
    })
  })

  test('the Gates tab shows the matrix', async () => {
    fakeFetch({ '/api/meta': {}, '/api/changes': CHANGES, '/api/changes/c1/trace': traceAtH2() })
    location.hash = '#c1/gates'
    renderApp()
    await push(stateAtH2())
    expect(await screen.findByText('greet.txt says hello')).toBeTruthy()
    expect(screen.getByText('no linked test')).toBeTruthy()
    expect(screen.getByText('T1')).toBeTruthy()
  })

  test('with no changes it offers to start one', async () => {
    fakeFetch({ '/api/meta': {}, '/api/changes': [] })
    renderApp()
    expect(await screen.findByText('No changes yet')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /Start a change/ }))
    expect(await screen.findByRole('dialog', { name: 'Start a change' })).toBeTruthy()
  })
})
