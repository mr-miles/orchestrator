import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '@/components/theme-provider'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SettingsProvider } from '@/lib/settings'
import App from './App'
import {
  CHANGES,
  FakeEventSource,
  fakeFetch,
  install,
  run,
  stateAtH1,
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

  test('with no changes it offers to start one', async () => {
    fakeFetch({ '/api/meta': {}, '/api/changes': [] })
    renderApp()
    expect(await screen.findByText('No changes yet')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /Start a change/ }))
    expect(await screen.findByRole('dialog', { name: 'Start a change' })).toBeTruthy()
  })
})
