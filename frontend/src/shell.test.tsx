import { afterEach, beforeEach, describe, expect, mock, test } from 'bun:test'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '@/components/theme-provider'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SettingsProvider } from '@/lib/settings'
import type { ChangeSummary } from '@/lib/types'
import App from './App'
import {
  attentionAtH1,
  CHANGES,
  FakeEventSource,
  fakeFetch,
  install,
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

const WORKSPACE = {
  id: 'app',
  name: 'Demo',
  repo: 'app',
  base_branch: 'main',
  source: null,
  publish_prefix: 'agent/',
  changes: { ACTIVE: 1, READY: 1 },
  documents: 3,
}

const MORE: ChangeSummary[] = [
  { ...CHANGES[0]!, workspace: 'app', needs_you: 1 },
  {
    id: 'c0',
    workspace: 'app',
    goal: 'Say goodbye',
    status: 'READY',
    tier: 'R1',
    created_at: '2025-12-01',
    accepted: 4,
    nodes: 4,
    live_runs: 0,
  },
]

let calls: Call[]

function routes(extra: Record<string, unknown> = {}) {
  calls = fakeFetch({
    '/api/meta': { project: 'demo', workspaces: ['app'], decision_owners: ['miles'] },
    '/api/workspaces': [WORKSPACE],
    '/api/changes': MORE,
    ...extra,
  })
}

/** The overview stream (pages without a change): the change list and attention. */
async function overview() {
  const es = await waitFor(() => FakeEventSource.last('/api/events'))
  act(() => {
    es.open()
    es.emit('changes', MORE)
    es.emit('attention', attentionAtH1())
  })
}

beforeEach(() => {
  install()
  localStorage.clear()
  sessionStorage.clear()
  location.hash = ''
  routes()
})

afterEach(cleanup)

describe('the shell', () => {
  test('work counts changes by status, filters, and shows them by workspace or all', async () => {
    renderApp()
    await overview()
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(crumbs.textContent).toBe('WorkloadsWork')
    const tiles = screen.getByRole('group', { name: 'Changes by status' })
    expect(within(tiles).getByRole('button', { name: /Need you\s*1/ })).toBeTruthy()
    expect(within(tiles).getByRole('button', { name: /ready\s*1/ })).toBeTruthy()
    // by workspace: a header row per workspace, linking to it
    const main = screen.getByRole('main')
    const group = within(main).getByRole('link', { name: 'Demo' })
    expect(group.getAttribute('href')).toBe('#/ws/app')
    expect(screen.getByRole('link', { name: 'c1' }).getAttribute('href')).toBe(
      '#/ws/app/c/c1/overview',
    )
    await userEvent.click(within(tiles).getByRole('button', { name: /ready\s*1/ }))
    expect(screen.queryByRole('link', { name: 'c1' })).toBeNull()
    expect(screen.getByRole('link', { name: 'c0' })).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Show all' }))
    await userEvent.click(screen.getByRole('button', { name: 'All changes' }))
    expect(screen.getByRole('columnheader', { name: 'Workspace' })).toBeTruthy()
    expect(localStorage.getItem('work-view')).toBe('all')
  })

  test('the menu collapses to its icons, keeps every link, and is remembered', async () => {
    location.hash = '#/inbox'
    const { unmount } = renderApp()
    await overview()
    const menu = () => screen.getAllByRole('navigation', { name: 'Main' })[0]!
    await userEvent.click(within(menu()).getByRole('button', { name: 'Collapse the menu' }))
    expect(localStorage.getItem('menu')).toBe('collapsed')
    const inbox = within(menu()).getByRole('link', { name: /^Inbox/ })
    expect(inbox.getAttribute('title')).toBe('Inbox') // named by its tooltip
    expect(inbox.querySelector('.bg-destructive')).toBeTruthy() // what needs you, as a dot
    expect(
      within(menu())
        .getByRole('link', { name: /^Skills/ })
        .getAttribute('href'),
    ).toBe('#/config/skills')
    unmount()
    renderApp()
    await overview()
    const again = within(menu()).getByRole('button', { name: 'Expand the menu' })
    expect(again.getAttribute('aria-expanded')).toBe('false')
    await userEvent.click(again)
    expect(localStorage.getItem('menu')).toBe('expanded')
    expect(
      within(menu())
        .getByRole('link', { name: /^Inbox/ })
        .getAttribute('title'),
    ).toBeNull()
  })

  test('the menu has workloads and configuration; the inbox shows what needs you', async () => {
    location.hash = '#/inbox'
    renderApp()
    await overview()
    const menu = screen.getAllByRole('navigation', { name: 'Main' })[0]!
    for (const [name, href] of [
      [/^Work\s*\d*$/, '#/work'],
      [/^Demo/, '#/ws/app'],
      [/^Inbox/, '#/inbox'],
      [/^Skills/, '#/config/skills'],
      [/^Workflows/, '#/config/workflows'],
      [/^Agents/, '#/config/agents'],
    ] as const)
      expect(within(menu).getByRole('link', { name }).getAttribute('href')).toBe(href)
    expect(
      within(menu)
        .getByRole('link', { name: /^Inbox/ })
        .getAttribute('aria-current'),
    ).toBe('page')
    const needs = screen.getByRole('list', { name: 'Needs you' })
    expect(within(needs).getByText('H-1: Accept requirements & tier')).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: 'Mark all read' }))
    expect(JSON.parse(localStorage.getItem('inbox-read') ?? '[]')).toEqual([
      'c0|None|change_ended|READY|t',
    ])
    // the bell shares the read state
    expect(screen.getByRole('button', { name: 'Needs you: 1; inbox: 0 unread' })).toBeTruthy()
  })

  test('a change shows Workloads › workspace › change', async () => {
    location.hash = '#/ws/app/c/c1/overview'
    renderApp()
    const es = await waitFor(() => FakeEventSource.last('/api/changes/c1/events'))
    act(() => {
      es.open()
      es.emit('state', stateAtH1())
    })
    const crumbs = await screen.findByRole('navigation', { name: 'Breadcrumb' })
    await waitFor(() => expect(crumbs.textContent).toBe('WorkloadsDemoc1'))
    expect(within(crumbs).getByRole('link', { name: 'Workloads' }).getAttribute('href')).toBe(
      '#/work',
    )
    expect(within(crumbs).getByRole('link', { name: 'Demo' }).getAttribute('href')).toBe('#/ws/app')
    expect(await screen.findByRole('tab', { name: /Overview/ })).toBeTruthy()
  })

  test('a workspace browses its document store, with copy and download', async () => {
    const path = 'changes/c1/intake/requirements.md'
    routes({
      '/api/docs': [
        {
          path,
          version: 2,
          role: 'intake',
          author: 'r1',
          sha256: 's',
          created_at: 't',
          schema: 'markdown',
          schema_sha: 'x',
          attestation: `file:${path}@2`,
        },
      ],
      '/api/doc': { path, version: 2, content: '- REQ-1: greet', history: [] },
    })
    const write = mock(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: write },
      configurable: true,
    })
    location.hash = '#/ws/app/documents'
    renderApp()
    await overview()
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' }).textContent).toBe('WorkloadsDemo')
    const tree = await screen.findByRole('navigation', { name: 'Documents' })
    await userEvent.type(within(tree).getByRole('textbox', { name: 'Filter files' }), 'require')
    await userEvent.click(await within(tree).findByRole('button', { name: /requirements\.md/ }))
    const file = screen.getByRole('region', { name: 'File' })
    expect(await within(file).findByText('- REQ-1: greet')).toBeTruthy()
    expect(within(file).getByRole('link', { name: 'Download' }).getAttribute('href')).toBe(
      `/api/doc/raw?path=${encodeURIComponent(path)}&version=2`,
    )
    await userEvent.click(within(file).getByRole('button', { name: 'Copy' }))
    expect(write).toHaveBeenCalledWith('- REQ-1: greet')
  })

  test('a workspace browses its repository at a branch', async () => {
    routes({
      '/api/workspaces/app/git/refs': [
        { name: 'main', sha: 'a'.repeat(40), base: true },
        { name: 'legion/c1/integration', sha: 'b'.repeat(40), base: false },
      ],
      '/api/workspaces/app/git/tree': {
        ref: 'main',
        sha: 'a'.repeat(40),
        truncated: false,
        files: [
          { path: 'README.md', size: 6 },
          { path: 'src/greet.txt', size: 6 },
        ],
      },
      '/api/workspaces/app/git/file': { path: 'src/greet.txt', size: 6, text: 'hello\n' },
    })
    location.hash = '#/ws/app/repository'
    renderApp()
    await overview()
    const tree = await screen.findByRole('navigation', { name: 'Repository files' })
    await userEvent.click(await within(tree).findByRole('button', { name: 'src' }))
    await userEvent.click(within(tree).getByRole('button', { name: /greet\.txt/ }))
    const file = screen.getByRole('region', { name: 'File' })
    expect(await within(file).findByText('hello')).toBeTruthy()
    expect(within(file).getByRole('link', { name: 'Download' }).getAttribute('href')).toBe(
      `/api/workspaces/app/git/file?ref=${'a'.repeat(40)}&path=src%2Fgreet.txt&download=1`,
    )
    expect(calls.some((c) => c.url === '/api/workspaces/app/git/tree?ref=main')).toBe(true)
  })

  test('configuration shows the definition files of a group', async () => {
    routes({
      '/api/config/skills/file': { path: 'skills/git-protocol.md', text: '## Skill: Git protocol' },
      '/api/config/skills': {
        group: 'skills',
        root: '/srv/assets',
        assets_sha: 'sha256:0123456789abcdef0123',
        files: [{ path: 'skills/git-protocol.md', size: 20 }],
      },
    })
    location.hash = '#/config/skills'
    renderApp()
    await overview()
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' }).textContent).toBe(
      'ConfigurationSkills',
    )
    expect(await screen.findByText('/srv/assets')).toBeTruthy()
    const tree = screen.getByRole('navigation', { name: 'Skills files' })
    await userEvent.click(within(tree).getByRole('button', { name: 'skills' }))
    await userEvent.click(within(tree).getByRole('button', { name: /git-protocol\.md/ }))
    const file = screen.getByRole('region', { name: 'File' })
    expect(await within(file).findByText('## Skill: Git protocol')).toBeTruthy()
    expect(within(file).getByRole('link', { name: 'Download' }).getAttribute('href')).toBe(
      '/api/config/skills/file?path=skills%2Fgit-protocol.md&download=1',
    )
  })
})
