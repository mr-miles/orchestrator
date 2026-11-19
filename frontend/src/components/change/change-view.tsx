import { useMemo, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { isLive } from '@/lib/status'
import { docsVersion } from '@/lib/trace'
import type { ChangeState } from '@/lib/types'
import { AgentsTab } from './agents-tab'
import { DocSheet, type DocRef } from './doc-sheet'
import { DocumentsTab } from './documents-tab'
import { GatesTab } from './gates-tab'
import { LogSheet } from './log-sheet'
import { NavContext, TABS, type Nav, type TabId } from './nav'
import { NodeSheet } from './node-sheet'
import { OverviewTab } from './overview-tab'
import { PlanTab } from './plan-tab'
import { WorkflowTab } from './workflow-tab'

/** One change: its tabs, and sheets for a node, an agent log or a document over them. */
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
  /** A node to open (from a link or an attention item); ``n`` changes on every request. */
  focus?: { node: string; n: number } | null
}) {
  const [node, setNode] = useState<string | null>(focus?.node ?? null)
  // a new request to open a node (React's "adjust state when a prop changes")
  const [focused, setFocused] = useState(focus?.n)
  if (focus && focus.n !== focused) {
    setFocused(focus.n)
    setNode(focus.node)
  }
  const [log, setLog] = useState<string | null>(null)
  const [doc, setDoc] = useState<DocRef | null>(null)
  const nav = useMemo<Nav>(
    () => ({
      cid,
      tab,
      setTab,
      openNode: setNode,
      openLog: setLog,
      openDoc: (path, version) => setDoc({ path, version }),
      openDiff: (path, from) => setDoc({ path, diffFrom: from }),
    }),
    [cid, tab, setTab],
  )
  const live = state.runs.filter(isLive).length
  const awaiting = state.nodes.filter(
    (n) => n.status === 'AWAITING_HUMAN' || n.status === 'ESCALATED',
  )
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
          <OverviewTab state={state} selected={node} />
        </TabsContent>
        <TabsContent value="agents">
          <AgentsTab runs={state.runs} />
        </TabsContent>
        <TabsContent value="gates">
          <GatesTab state={state} />
        </TabsContent>
        <TabsContent value="plan">
          <PlanTab state={state} />
        </TabsContent>
        <TabsContent value="documents">
          <DocumentsTab version={docsVersion(state)} />
        </TabsContent>
        <TabsContent value="workflow">
          <WorkflowTab assetsSha={state.assets_sha} />
        </TabsContent>
      </Tabs>
      <NodeSheet state={state} nodeId={node} onClose={() => setNode(null)} />
      <LogSheet run={log} onClose={() => setLog(null)} />
      <DocSheet doc={doc} onOpen={setDoc} onClose={() => setDoc(null)} />
    </NavContext>
  )
}
