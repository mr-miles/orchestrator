import { useCallback, useEffect, useMemo, useState } from 'react'
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { ChangeView } from '@/components/change/change-view'
import type { TabId } from '@/components/change/nav'
import { Header, NewChangeDialog, type Crumb } from '@/components/header'
import { ConfigPage } from '@/components/pages/config-page'
import { InboxPage } from '@/components/pages/inbox-page'
import { WorkPage } from '@/components/pages/work-page'
import { WorkspacePage } from '@/components/pages/workspace-page'
import { AppSidebar } from '@/components/shell/sidebar'
import { getChanges, getMeta, getWorkspaces } from '@/lib/api'
import { useAttentionTitle } from '@/lib/attention'
import { formatHash, parseHash, samePage, type Route } from '@/lib/route'
import { useChangeStream, useOverviewStream } from '@/lib/streams'
import type { AttentionItem, ChangeSummary, Meta, Workspace } from '@/lib/types'
import { cn } from '@/lib/utils'

const MENU_KEY = 'menu'
const GROUP_LABEL = { skills: 'Skills', workflows: 'Workflows', agents: 'Agents' } as const

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseHash(location.hash))
  const [meta, setMeta] = useState<Meta | null>(null)
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [initial, setInitial] = useState<ChangeSummary[] | null>(null)
  const [creating, setCreating] = useState(false)
  const [menu, setMenu] = useState(false)
  // the left-hand menu collapsed to its icons (remembered in this browser)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(MENU_KEY) === 'collapsed')
  const toggleMenu = () =>
    setCollapsed((c) => {
      localStorage.setItem(MENU_KEY, c ? 'expanded' : 'collapsed')
      return !c
    })
  useEffect(() => {
    getMeta().then(setMeta, () => undefined)
    getWorkspaces().then(setWorkspaces, () => undefined)
    getChanges().then(setInitial, () => setInitial([]))
    // dropping a ticket from the address (below) is not a new page
    const onHash = () =>
      setRoute((prev) => {
        const r = parseHash(location.hash)
        return (r.page === 'change' && r.focus) || !samePage(r, prev) ? r : prev
      })
    addEventListener('hashchange', onHash)
    return () => removeEventListener('hashchange', onHash)
  }, [])

  const onChange = route.page === 'change' ? route.cid : null
  const changeStream = useChangeStream(onChange)
  const overview = useOverviewStream(!onChange)
  const live = onChange ? changeStream : overview
  const streamed = live.changes
  const changes = useMemo(() => streamed ?? initial ?? [], [streamed, initial])
  const attention = live.attention
  const defaultWs = workspaces[0]?.id ?? meta?.workspaces?.[0] ?? ''
  const wsOf = useCallback(
    (cid: string) => changes.find((c) => c.id === cid)?.workspace ?? defaultWs,
    [changes, defaultWs],
  )
  // an old-style change link names no workspace: it is the change's
  const resolved: Route = useMemo(
    () =>
      route.page === 'change' && !route.ws && defaultWs ? { ...route, ws: wsOf(route.cid) } : route,
    [route, defaultWs, wsOf],
  )

  // the address shows the page (a ticket opens once; an old-style link becomes the new form)
  useEffect(() => {
    if (resolved.page === 'change' && !resolved.ws) return
    const h = formatHash(resolved)
    if (location.hash !== h) history.replaceState(null, '', h)
  }, [resolved])

  const go = useCallback((r: Route, ticket?: string | null) => {
    location.hash = formatHash(r) + (ticket ? `/${encodeURIComponent(ticket)}` : '')
  }, [])
  const openItem = useCallback(
    (i: AttentionItem) =>
      go(
        { page: 'change', ws: wsOf(i.change), cid: i.change, tab: 'overview', focus: null },
        i.ticket,
      ),
    [go, wsOf],
  )
  const setTab = useCallback(
    (t: TabId) => resolved.page === 'change' && go({ ...resolved, tab: t, focus: null }),
    [go, resolved],
  )
  useAttentionTitle(attention?.needs_you.length ?? 0)

  const wsName = (id: string) => workspaces.find((w) => w.id === id)?.name || id
  const work: Crumb = { label: 'Workloads', href: formatHash({ page: 'work' }) }
  const crumbs: Crumb[] =
    resolved.page === 'work'
      ? [work, { label: 'Work' }]
      : resolved.page === 'inbox'
        ? [work, { label: 'Inbox' }]
        : resolved.page === 'workspace'
          ? [work, { label: wsName(resolved.ws) }]
          : resolved.page === 'change'
            ? [
                work,
                {
                  label: wsName(resolved.ws),
                  href: formatHash({ page: 'workspace', ws: resolved.ws, tab: 'changes' }),
                },
                { label: resolved.cid, mono: true },
              ]
            : [{ label: 'Configuration' }, { label: GROUP_LABEL[resolved.group] }]
  const current = resolved.page === 'change' ? changes.find((c) => c.id === resolved.cid) : null

  const sidebar = (
    onNavigate?: () => void,
    rail?: { collapsed: boolean; onToggle: () => void },
  ) => (
    <AppSidebar
      route={resolved}
      project={meta?.project ?? ''}
      workspaces={workspaces}
      changes={changes}
      attention={attention}
      onNavigate={onNavigate}
      collapsed={rail?.collapsed}
      onToggle={rail?.onToggle}
    />
  )
  return (
    <div className="flex min-h-svh">
      <aside
        className={cn(
          'sticky top-0 hidden h-svh shrink-0 overflow-y-auto border-r transition-[width] md:block',
          collapsed ? 'w-14' : 'w-60',
        )}
      >
        {sidebar(undefined, { collapsed, onToggle: toggleMenu })}
      </aside>
      <Sheet open={menu} onOpenChange={setMenu}>
        <SheetContent side="left" className="w-64 p-0">
          <SheetTitle className="sr-only">Menu</SheetTitle>
          {sidebar(() => setMenu(false))}
        </SheetContent>
      </Sheet>
      <div className="flex min-w-0 flex-1 flex-col">
        <Header
          crumbs={crumbs}
          status={current?.status}
          connection={live.connection}
          attention={attention}
          onOpenItem={openItem}
          onMenu={() => setMenu(true)}
        />
        <main className="mx-auto w-full max-w-screen-2xl flex-1 p-4">
          {!initial ? (
            <Skeleton className="h-96 w-full" />
          ) : resolved.page === 'work' ? (
            <WorkPage
              changes={changes}
              workspaces={workspaces}
              defaultWorkspace={defaultWs}
              onNew={() => setCreating(true)}
              onOpenItem={openItem}
            />
          ) : resolved.page === 'inbox' ? (
            <InboxPage attention={attention} onOpen={openItem} />
          ) : resolved.page === 'workspace' ? (
            <WorkspacePage
              ws={resolved.ws}
              tab={resolved.tab}
              workspaces={workspaces}
              changes={changes}
            />
          ) : resolved.page === 'config' ? (
            <ConfigPage group={resolved.group} />
          ) : changeStream.state?.change ? (
            <ChangeView
              cid={resolved.cid}
              state={changeStream.state}
              tab={resolved.tab}
              setTab={setTab}
              focus={resolved.focus}
            />
          ) : (
            <Skeleton className="h-96 w-full" />
          )}
        </main>
      </div>
      <NewChangeDialog
        open={creating}
        onOpenChange={setCreating}
        workspaces={workspaces}
        workflows={meta?.workflows}
        defaultWorkflow={meta?.default_workflow}
        defaultWorkspace={
          resolved.page === 'workspace' || resolved.page === 'change' ? resolved.ws : defaultWs
        }
        onStarted={(c, ws) => {
          setInitial((xs) =>
            xs?.some((x) => x.id === c)
              ? xs
              : [{ id: c, workspace: ws } as ChangeSummary, ...(xs ?? [])],
          )
          go({ page: 'change', ws, cid: c, tab: 'overview', focus: null })
        }}
      />
    </div>
  )
}
