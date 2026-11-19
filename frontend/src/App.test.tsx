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
