import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '@/components/theme-provider'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SettingsProvider } from '@/lib/settings'
import App from './App'
import { VerificationDefaults, VerificationRunDefaults } from './lib/api.gen'
import {
  attentionAtH1,
  CHANGES,
  FakeEventSource,
  fakeFetch,
  install,
  run,
  stateAtH1,
  stateWaiting,
  ticket,
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
  location.hash = '#/ws/app/c/c1/overview' // most tests open the change
  calls = fakeFetch({
    '/api/meta': {
      project: 'demo',
      workspaces: ['app'],
      decision_owners: ['miles'],
      assets_sha: 'sha256:abc',
      live_runs: 0,
      max_parallel: 4,
    },
    '/api/changes/c1/tickets/H-1/action': { status: 'passed' },
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
    expect(screen.getByRole('tab', { name: /Tickets.*1/ })).toBeTruthy() // H-1 is due
    expect(screen.getByText('Live')).toBeTruthy()
    // the sign-off's own pause is not the operator's: no Resume, what it waits for instead
    expect(screen.getByText(/waiting for H-1's sign-off/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
    const next = stateAtH1()
    next.signoffs = {}
    next.tickets = next.tickets.map((t) => (t.id === 'H-1' ? { ...t, status: 'passed' } : t))
    act(() => es.emit('state', next))
    await waitFor(() => expect(screen.queryByRole('tab', { name: /Tickets.*1/ })).toBeNull())
    expect(calls.filter((c) => c.url.includes('/state'))).toEqual([]) // never fetched
  })

  test('the tree lists the tickets in the order they run, criteria under their owner', async () => {
    renderApp()
    await push()
    const tree = await screen.findByRole('list', { name: 'Tickets' })
    const rows = within(tree)
      .getAllByRole('button')
      .filter((b) => !b.hasAttribute('aria-expanded'))
      .map((b) => b.querySelector('.font-mono')?.textContent)
    expect(rows).toEqual(['change', 'workspace', 'requirements', 'intake', 'H-1', 'plan'])
    expect(within(tree).getByText('sign-off due')).toBeTruthy()
    expect(within(tree).getByLabelText('criterion')).toBeTruthy()
  })

  test('a sign-off shows exactly what it signs and records the decision as the approver', async () => {
    localStorage.setItem('approver', 'miles')
    sessionStorage.setItem('optoken', 'tok')
    renderApp()
    await push()
    const tree = await screen.findByRole('list', { name: 'Tickets' })
    await userEvent.click(within(tree).getByText('H-1', { selector: '.font-mono' }))
    const sheet = await screen.findByRole('dialog')
    expect(within(sheet).getByText('Sign-off due')).toBeTruthy()
    expect(
      within(sheet).getAllByText('file:changes/c1/tickets/intake/requirements.md@1').length,
    ).toBeGreaterThan(0)
    const reject = within(sheet).getByRole('button', { name: 'Reject' }) as HTMLButtonElement
    expect(reject.disabled).toBe(true) // a rejection needs a reason
    await userEvent.click(within(sheet).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/tickets/H-1/action'))).toBe(true))
    expect(calls.find((c) => c.url.endsWith('/tickets/H-1/action'))?.body).toEqual({
      action: 'approve',
      approver: 'miles',
      payload: { reason: '' },
    })
  })

  test('the agents tab lists live runs and opens their log stream', async () => {
    renderApp()
    const state = stateAtH1()
    state.runs.push(
      run({
        id: 'r0002',
        ticket: 'design',
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

  test("a ticket's verification opens its log, live while it runs", async () => {
    location.hash = '#c1/overview/verify:T1'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    const s = stateAtH1()
    const verify = ticket({
      id: 'verify:T1',
      type: 'controller:verify',
      parent: 'design',
      role: 'criterion',
      status: 'working',
      attempt: 2,
    })
    const earlier = {
      ...VerificationRunDefaults,
      id: 'v-1',
      key: 'k',
      sha: 'b'.repeat(40),
      commands: ['ruff check'],
      timeout_s: 60,
      node: 'verify:T1',
      state: 'failed' as const,
      attempt: 1,
      requested_by: [],
      priority: 0,
      result: { ...VerificationDefaults, sha: 'b'.repeat(40), ok: false, log: 'lgv-old' },
    }
    await act(async () => {
      es.open()
      es.emit('state', {
        ...s,
        tickets: [...s.tickets, verify],
        verifying: { 'verify:T1': 'lgv-now' },
        verifications: [earlier],
      })
    })
    const sheet = await screen.findByRole('dialog')
    expect(within(sheet).getByText('verifying now')).toBeTruthy()
    const logs = within(sheet).getAllByRole('button', { name: /Log/ })
    expect(logs).toHaveLength(2) // the live one, then the earlier verification's
    await userEvent.click(logs[0]!)
    await waitFor(() => FakeEventSource.last('/api/runs/lgv-now/log/events'))
  })

  test("an agent's question is answered from the tickets tab as the approver", async () => {
    localStorage.setItem('approver', 'miles')
    calls = fakeFetch({
      '/api/changes/c1/questions/t-1/answer': { id: 't-1', answer: 'Yes' },
      '/api/changes': CHANGES,
    })
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    const q = {
      id: 't-1',
      ticket: 'critique',
      role: 'critic',
      run: 'r0003',
      question: 'May the greeting be in French?',
      asked_at: '2026-11-22T10:00:00.000000Z',
      answer: null,
      answered_by: null,
      answered_at: null,
      withdrawn: null,
    }
    await act(async () => {
      es.open()
      es.emit('state', { ...stateAtH1(), a2a: { cards: [], questions: [q] } })
    })
    expect(await screen.findByText('May the greeting be in French?')).toBeTruthy()
    await userEvent.type(screen.getByRole('textbox', { name: 'Answer to critic@critique' }), 'Yes')
    await userEvent.click(screen.getByRole('button', { name: 'Answer' }))
    await waitFor(() =>
      expect(calls.find((c) => c.url === '/api/changes/c1/questions/t-1/answer')?.body).toEqual({
        answer: 'Yes',
        approver: 'miles',
      }),
    )
  })

  test("a build's request for more files shows its files before and after, then is decided", async () => {
    localStorage.setItem('approver', 'miles')
    calls = fakeFetch({
      '/api/changes/c1/questions/t-2/answer': { id: 't-2', approved: true },
      '/api/changes': CHANGES,
    })
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    const q = {
      id: 't-2',
      ticket: 'T1',
      role: 'implementer',
      run: 'r0007',
      question: 'the new endpoint must be registered in the route table',
      kind: 'scope',
      paths: ['src/api/routes.py'],
      before: ['src/api/users/'],
      warnings: ['T2 (working) also owns src/api/routes.py'],
      approved: null,
      asked_at: '2026-11-22T10:00:00.000000Z',
      answer: null,
      answered_by: null,
      answered_at: null,
      withdrawn: null,
    }
    await act(async () => {
      es.open()
      es.emit('state', { ...stateAtH1(), a2a: { cards: [], questions: [q] } })
    })
    const diff = await screen.findByLabelText('files_modified before and after')
    expect(within(diff).getAllByText('src/api/users/', { exact: false })).toHaveLength(2)
    expect(within(diff).getByText('src/api/routes.py', { exact: false }).textContent).toContain('+')
    expect(screen.getByText('T2 (working) also owns src/api/routes.py')).toBeTruthy()
    const deny = screen.getByRole('button', { name: 'Deny' }) as HTMLButtonElement
    expect(deny.disabled).toBe(true) // a denial needs a reason
    await userEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() =>
      expect(calls.find((c) => c.url === '/api/changes/c1/questions/t-2/answer')?.body).toEqual({
        answer: '',
        approver: 'miles',
        decision: 'approve',
      }),
    )
  })

  test("the resolver's proposal on a waiting ticket is applied as the approver", async () => {
    localStorage.setItem('approver', 'miles')
    calls = fakeFetch({
      '/api/changes/c1/tickets/T1/action': { status: 'checking' },
      '/api/changes': CHANGES,
    })
    location.hash = '#c1/overview/T1'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    await act(async () => {
      es.open()
      es.emit('state', stateWaiting())
    })
    const sheet = await screen.findByRole('dialog')
    expect(within(sheet).getByText(/the same findings came back/)).toBeTruthy()
    expect(
      within(sheet).getByText('The finding asks for something no requirement says.'),
    ).toBeTruthy()
    expect(within(sheet).getByRole('button', { name: 'Waive review:T1' })).toBeTruthy()
    expect(within(sheet).queryByRole('button', { name: 'Retry' })).toBeNull() // not a remedy here
    await userEvent.click(within(sheet).getByRole('button', { name: 'Apply proposal' }))
    await waitFor(() =>
      expect(calls.find((c) => c.url === '/api/changes/c1/tickets/T1/action')?.body).toEqual({
        action: 'waive:review:T1',
        approver: 'miles',
        payload: { reason: 'the newline is not a requirement' },
      }),
    )
  })

  test('a waiver takes a reason and goes to the waiting ticket', async () => {
    localStorage.setItem('approver', 'miles')
    calls = fakeFetch({
      '/api/changes/c1/tickets/T1/action': { status: 'checking' },
      '/api/changes': CHANGES,
    })
    location.hash = '#c1/overview/T1'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    await act(async () => {
      es.open()
      es.emit('state', stateWaiting())
    })
    const sheet = await screen.findByRole('dialog')
    await userEvent.click(within(sheet).getByRole('button', { name: 'Waive review:T1' }))
    const form = await screen.findByRole('dialog', { name: 'Waive review:T1' })
    await userEvent.type(within(form).getByRole('textbox', { name: 'Reason' }), 'checked by hand')
    await userEvent.click(within(form).getByRole('button', { name: 'Waive' }))
    await waitFor(() =>
      expect(calls.find((c) => c.url === '/api/changes/c1/tickets/T1/action')?.body).toEqual({
        action: 'waive:review:T1',
        approver: 'miles',
        payload: { reason: 'checked by hand' },
      }),
    )
  })

  test('the change is paused and resumed from the tickets tab', async () => {
    localStorage.setItem('approver', 'miles')
    calls = fakeFetch({ '/api/changes/c1/action': { note: 'pause' }, '/api/changes': CHANGES })
    renderApp()
    const es = await push(stateWaiting())
    await userEvent.click(await screen.findByRole('button', { name: 'Pause' }))
    await waitFor(() =>
      expect(calls.find((c) => c.url === '/api/changes/c1/action')?.body).toEqual({
        action: 'pause',
        approver: 'miles',
        payload: {},
      }),
    )
    act(() => es.emit('state', { ...stateWaiting(), paused: ['operator'] }))
    expect(await screen.findByRole('button', { name: 'Resume' })).toBeTruthy()
    expect(screen.getByText(/paused by a decision owner/)).toBeTruthy()
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
    await userEvent.click(within(menu).getByText('H-1: sign off requirements, risk'))
    const sheet = await screen.findByRole('dialog')
    expect(within(sheet).getByText('Sign-off due')).toBeTruthy()
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

  test('a link with a ticket opens its sheet once', async () => {
    location.hash = '#c1/overview/H-1'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    await act(async () => {
      es.open()
      es.emit('state', stateAtH1()) // the sheet mounts open: let its effects settle in act
    })
    const sheet = await screen.findByRole('dialog')
    expect(await within(sheet).findByText('Sign-off due')).toBeTruthy()
    expect(location.hash).toBe('#/ws/app/c/c1/overview') // the old-style link, in its workspace
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
    location.hash = '#/work' // shown where you land
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/events'))
    act(() => {
      es.open()
      es.emit('changes', CHANGES)
    })
    expect(await screen.findByText('While you were away')).toBeTruthy()
    expect(screen.getByText(/2 runs started/)).toBeTruthy()
    expect(calls.some((c) => c.url.startsWith('/api/digest?since='))).toBe(true)
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByText('While you were away')).toBeNull())
  })

  test('with no changes it offers to start one', async () => {
    fakeFetch({ '/api/meta': {}, '/api/changes': [] })
    location.hash = ''
    renderApp()
    expect(await screen.findByText('No changes yet: start one.')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /New change/ }))
    expect(await screen.findByRole('dialog', { name: 'Start a change' })).toBeTruthy()
  })
})
