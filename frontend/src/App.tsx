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
import { AwayDigest } from '@/components/away-digest'
import { ChangeView } from '@/components/change/change-view'
import { TABS, type TabId } from '@/components/change/nav'
import { Header, NewChangeDialog } from '@/components/header'
import { getChanges, getMeta } from '@/lib/api'
import { useAttentionTitle } from '@/lib/attention'
import { useChangeStream } from '@/lib/streams'
import type { AttentionItem, ChangeSummary, Meta } from '@/lib/types'

interface Route {
  cid: string | null
  tab: TabId
  /** A node to open, from ``#<change>/<tab>/<node>`` (a notification's link) or an item. */
  focus: { node: string; n: number } | null
}

let focusCount = 0

/** ``#<change>/<tab>[/<node>]``: the open change and tab, so a link or reload comes back to
 * them; a node opens its sheet (once: the hash then drops it). */
function readHash(): Route {
  const [cid, tab, node] = decodeURIComponent(location.hash.slice(1)).split('/')
  const known = TABS.find((t) => t.id === tab)?.id
  return {
    cid: cid || localStorage.getItem('cid'),
    tab: known ?? 'overview',
    focus: node ? { node, n: ++focusCount } : null,
  }
}

export default function App() {
  const [route, setRoute] = useState(readHash)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [initial, setInitial] = useState<ChangeSummary[] | null>(null)
  const [creating, setCreating] = useState(false)
  useEffect(() => {
    getMeta().then(setMeta, () => undefined)
    getChanges().then(setInitial, () => setInitial([]))
    // dropping the node from the address (below) is not a new route
    const onHash = () =>
      setRoute((prev) => {
        const r = readHash()
        return r.focus || r.cid !== prev.cid || r.tab !== prev.tab ? r : prev
      })
    addEventListener('hashchange', onHash)
    return () => removeEventListener('hashchange', onHash)
  }, [])
  const go = useCallback((cid: string, tab: TabId, node?: string | null) => {
    localStorage.setItem('cid', cid)
    history.replaceState(null, '', `#${encodeURIComponent(cid)}/${tab}`)
    setRoute({ cid, tab, focus: node ? { node, n: ++focusCount } : null })
  }, [])
  // a node from the hash is opened once; the address keeps only the change and tab
  useEffect(() => {
    if (route.cid && location.hash.slice(1).split('/').length > 2)
      history.replaceState(null, '', `#${encodeURIComponent(route.cid)}/${route.tab}`)
  }, [route])
  const changes0 = initial ?? []
  // a remembered change that is gone falls back to the newest
  const cid = changes0.some((c) => c.id === route.cid) ? route.cid : (changes0[0]?.id ?? null)
  const stream = useChangeStream(initial ? cid : null)
  const changes = stream.changes ?? changes0
  const setTab = useCallback((t: TabId) => cid && go(cid, t), [cid, go])
  const openItem = useCallback(
    (i: AttentionItem) => {
      setInitial((xs) =>
        xs?.some((x) => x.id === i.change)
          ? xs
          : [{ id: i.change } as ChangeSummary, ...(xs ?? [])],
      )
      go(i.change, 'overview', i.node)
    },
    [go],
  )
  useAttentionTitle(stream.attention?.needs_you.length ?? 0)
  return (
    <div className="min-h-svh">
      <Header
        project={meta?.project ?? ''}
        changes={changes}
        cid={cid}
        connection={cid ? stream.connection : null}
        attention={stream.attention}
        onPick={(c) => go(c, route.tab)}
        onNew={() => setCreating(true)}
        onOpenItem={openItem}
      />
      <main className="mx-auto max-w-screen-2xl p-4">
        {initial && cid && <AwayDigest onOpen={openItem} />}
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
          <ChangeView
            cid={cid}
            state={stream.state}
            tab={route.tab}
            setTab={setTab}
            focus={route.cid === cid ? route.focus : null}
          />
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
