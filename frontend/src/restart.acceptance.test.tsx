/** Acceptance (REQ-7, AC-9): the ticket sheet offers Restart – behind a confirmation – on a
 * waiting build, never on a waiting grouping ticket or an empty one; confirming posts
 * {action: 'restart', approver, payload: {}} to the ticket's action endpoint. */
import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '@/components/theme-provider'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SettingsProvider } from '@/lib/settings'
import App from './App'
import type { ChangeState, Ticket } from './lib/types'
import {
  CHANGES,
  FakeEventSource,
  fakeFetch,
  install,
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

let calls: Call[]

beforeEach(() => {
  install()
  localStorage.clear()
  sessionStorage.clear()
  localStorage.setItem('approver', 'miles')
  calls = fakeFetch({
    '/api/changes/c1/tickets/T1/action': { status: 'working' },
    '/api/changes': CHANGES,
  })
})

afterEach(cleanup)

/** The waiting-build state of the repository's own fakes, with ``extra`` tickets added. */
function stateWith(...extra: Ticket[]): ChangeState {
  const s = stateWaiting()
  return { ...s, tickets: [...s.tickets, ...extra] }
}

async function openSheet(tid: string, state: ChangeState) {
  location.hash = `#c1/overview/${tid}`
  renderApp()
  const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
  await act(async () => {
    es.open()
    es.emit('state', state)
  })
  return await screen.findByRole('dialog')
}

describe('restart, as the ticket sheet offers it', () => {
  test('AC-9: a waiting build shows Restart, confirmation first, then posts the action', async () => {
    const sheet = await openSheet('T1', stateWith())
    const button = await within(sheet).findByRole('button', { name: 'Restart' })
    await userEvent.click(button)
    const confirm = await screen.findByRole('alertdialog')
    // nothing is sent before the confirmation
    expect(calls.find((c) => c.url === '/api/changes/c1/tickets/T1/action')).toBeUndefined()
    await userEvent.click(within(confirm).getByRole('button', { name: /restart/i }))
    await waitFor(() =>
      expect(calls.find((c) => c.url === '/api/changes/c1/tickets/T1/action')?.body).toEqual({
        action: 'restart',
        approver: 'miles',
        payload: {},
      }),
    )
  })

  test('AC-9: cancelling the confirmation sends nothing', async () => {
    const sheet = await openSheet('T1', stateWith())
    await userEvent.click(await within(sheet).findByRole('button', { name: 'Restart' }))
    const confirm = await screen.findByRole('alertdialog')
    await userEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    await new Promise((r) => setTimeout(r, 50))
    expect(calls.find((c) => c.url === '/api/changes/c1/tickets/T1/action')).toBeUndefined()
  })

  test('AC-9: a waiting grouping ticket (escalated) has no Restart', async () => {
    const group = ticket({
      id: 'G1',
      type: 'grouping',
      parent: 'plan',
      status: 'waiting',
      waiting: 'escalated',
      subtasks: [['T1']],
      criteria: ['sign:G1'],
    })
    const sign = ticket({
      id: 'sign:G1',
      type: 'agent:reviewer',
      parent: 'G1',
      role: 'criterion',
      status: 'rejected',
      waivable: true,
    })
    const sheet = await openSheet('G1', stateWith(group, sign))
    await within(sheet).findByText(/Waiting on a human/)
    expect(within(sheet).queryByRole('button', { name: 'Restart' })).toBeNull()
  })

  test('AC-9: a waiting empty grouping ticket has no Restart', async () => {
    const empty = ticket({
      id: 'E1',
      type: 'grouping',
      parent: 'plan',
      status: 'waiting',
      waiting: 'empty',
    })
    const sheet = await openSheet('E1', stateWith(empty))
    await within(sheet).findByText(/Waiting on a human/)
    expect(within(sheet).queryByRole('button', { name: 'Restart' })).toBeNull()
  })
})
