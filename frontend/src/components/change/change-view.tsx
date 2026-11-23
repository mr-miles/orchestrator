import { useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { isLive } from '@/lib/status'
import { docsVersion, needsDecision } from '@/lib/tickets'
import type { ChangeState } from '@/lib/types'
import { AgentsTab } from './agents-tab'
import { DocSheet, type DocRef } from './doc-sheet'
import { DocumentsTab } from './documents-tab'
import { LogSheet } from './log-sheet'
import { NavContext, TABS, type Nav, type TabId } from './nav'
import { OverviewTab } from './overview-tab'
import { TicketSheet } from './ticket-sheet'
import { WorkflowTab } from './workflow-tab'

/** One change: its tabs, and sheets for a ticket, an agent log or a document over them. */
export function ChangeView({
  cid,
  state,
  tab,
  setTab,
  focus,
}: {
  cid: string
  state: ChangeState
  tab: TabId
  setTab: (t: TabId) => void
  /** A ticket to open (from a link or an attention item); ``n`` changes on every request. */
  focus?: { ticket: string; n: number } | null
}) {
  const [ticket, setTicket] = useState<string | null>(focus?.ticket ?? null)
  // a new request to open a ticket (React's "adjust state when a prop changes")
  const [focused, setFocused] = useState(focus?.n)
  if (focus && focus.n !== focused) {
    setFocused(focus.n)
    setTicket(focus.ticket)
  }
  const [log, setLog] = useState<string | null>(null)
  const [doc, setDoc] = useState<DocRef | null>(null)
  const nav = useMemo<Nav>(
    () => ({
      cid,
      tab,
      setTab,
      openTicket: setTicket,
      openLog: setLog,
      openDoc: (path, version) => setDoc({ path, version }),
      openDiff: (path, from) => setDoc({ path, diffFrom: from }),
    }),
    [cid, tab, setTab],
  )
  const live = state.runs.filter(isLive).length
  const awaiting = needsDecision(state)
  return (
    <NavContext value={nav}>
      <Tabs value={tab} onValueChange={(v) => setTab(v as TabId)}>
        <TabsList>
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id}>
              {t.label}
              {t.id === 'agents' && live > 0 && <Badge variant="secondary">{live}</Badge>}
              {t.id === 'overview' && awaiting.length > 0 && (
                <Badge variant="destructive" title="waiting for a decision">
                  {awaiting.length}
                </Badge>
              )}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="overview">
          <OverviewTab state={state} selected={ticket} />
        </TabsContent>
        <TabsContent value="agents">
          <AgentsTab runs={state.runs} cards={state.a2a?.cards} />
        </TabsContent>
        <TabsContent value="documents">
          <DocumentsTab version={docsVersion(state)} />
        </TabsContent>
        <TabsContent value="workflow">
          <WorkflowTab assetsSha={state.assets_sha} />
        </TabsContent>
      </Tabs>
      <TicketSheet state={state} ticketId={ticket} onClose={() => setTicket(null)} />
      <LogSheet run={log} onClose={() => setLog(null)} />
      <DocSheet doc={doc} onOpen={setDoc} onClose={() => setDoc(null)} />
    </NavContext>
  )
}
