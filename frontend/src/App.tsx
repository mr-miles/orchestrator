import { PlusIcon } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { ChangeView } from '@/components/change/change-view'
import { TABS, type TabId } from '@/components/change/nav'
import { Header, NewChangeDialog } from '@/components/header'
import { getChanges, getMeta } from '@/lib/api'
import { useChangeStream } from '@/lib/streams'
import type { ChangeSummary, Meta } from '@/lib/types'

/** ``#<change>/<tab>``: the open change and tab, so a link or reload comes back to them. */
function readHash(): { cid: string | null; tab: TabId } {
  const [cid, tab] = decodeURIComponent(location.hash.slice(1)).split('/')
  const known = TABS.find((t) => t.id === tab)?.id
  return { cid: cid || localStorage.getItem('cid'), tab: known ?? 'overview' }
}

export default function App() {
  const [route, setRoute] = useState(readHash)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [initial, setInitial] = useState<ChangeSummary[] | null>(null)
  const [creating, setCreating] = useState(false)
  useEffect(() => {
    getMeta().then(setMeta, () => undefined)
    getChanges().then(setInitial, () => setInitial([]))
    const onHash = () => setRoute(readHash())
    addEventListener('hashchange', onHash)
    return () => removeEventListener('hashchange', onHash)
  }, [])
  const go = useCallback((cid: string, tab: TabId) => {
    localStorage.setItem('cid', cid)
    history.replaceState(null, '', `#${encodeURIComponent(cid)}/${tab}`)
    setRoute({ cid, tab })
  }, [])
  const changes0 = initial ?? []
  // a remembered change that is gone falls back to the newest
  const cid = changes0.some((c) => c.id === route.cid) ? route.cid : (changes0[0]?.id ?? null)
  const stream = useChangeStream(initial ? cid : null)
  const changes = stream.changes ?? changes0
  const setTab = useCallback((t: TabId) => cid && go(cid, t), [cid, go])
  return (
    <div className="min-h-svh">
      <Header
        project={meta?.project ?? ''}
        changes={changes}
        cid={cid}
        connection={cid ? stream.connection : null}
        onPick={(c) => go(c, route.tab)}
        onNew={() => setCreating(true)}
      />
      <main className="mx-auto max-w-screen-2xl p-4">
        {!initial ? (
          <Skeleton className="h-96 w-full" />
        ) : !cid ? (
          <Empty className="border">
            <EmptyHeader>
              <EmptyTitle>No changes yet</EmptyTitle>
              <EmptyDescription>Start one with a goal for the agents.</EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              <Button onClick={() => setCreating(true)}>
                <PlusIcon data-icon="inline-start" />
                Start a change
              </Button>
            </EmptyContent>
          </Empty>
        ) : stream.state?.change ? (
          <ChangeView cid={cid} state={stream.state} tab={route.tab} setTab={setTab} />
        ) : (
          <Skeleton className="h-96 w-full" />
        )}
      </main>
      <NewChangeDialog
        open={creating}
        onOpenChange={setCreating}
        onStarted={(c) => {
          setInitial((xs) =>
            xs?.some((x) => x.id === c) ? xs : [{ id: c } as ChangeSummary, ...(xs ?? [])],
          )
          go(c, 'overview')
        }}
      />
    </div>
  )
}
