import { afterEach, beforeEach, describe, expect, test } from 'bun:test'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NewChangeDialog } from '@/components/header'
import { SettingsProvider } from '@/lib/settings'
import type { Workspace } from '@/lib/types'
import { fakeFetch, install, type Call } from './test/fakes'

const WS = [{ id: 'app', name: 'Demo' }] as unknown as Workspace[]
let calls: Call[]

function renderDialog(workflows?: string[], defaultWorkflow?: string) {
  return render(
    <SettingsProvider>
      <NewChangeDialog
        open
        onOpenChange={() => {}}
        onStarted={() => {}}
        workspaces={WS}
        defaultWorkspace="app"
        workflows={workflows}
        defaultWorkflow={defaultWorkflow}
      />
    </SettingsProvider>,
  )
}

async function start() {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Goal'), 'Do it')
  await user.click(screen.getByRole('button', { name: 'Start intake' }))
  await waitFor(() => expect(calls.some((c) => c.url === '/api/changes')).toBe(true))
  return calls.find((c) => c.url === '/api/changes')!.body as Record<string, unknown>
}

describe('NewChangeDialog workflow', () => {
  beforeEach(() => {
    install()
    calls = fakeFetch({ '/api/changes': { change: 'c9' } })
  })
  afterEach(cleanup)

  test('no select for a single workflow; its default is still sent', async () => {
    renderDialog(['default'], 'default')
    expect(screen.queryByText('Workflow')).toBeNull()
    expect((await start()).workflow).toBe('default')
  })

  test('no workflow in the body when the list is absent', async () => {
    renderDialog()
    expect(screen.queryByText('Workflow')).toBeNull()
    const body = await start()
    expect('workflow' in body).toBe(false)
  })

  test('several workflows show the select, preselecting the default, and send it', async () => {
    renderDialog(['default', 'design-review'], 'design-review')
    expect(screen.getByText('Workflow')).toBeTruthy()
    const body = await start()
    expect(body).toEqual({ goal: 'Do it', workspace: 'app', workflow: 'design-review' })
  })
})
