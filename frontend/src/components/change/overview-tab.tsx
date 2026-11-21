import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { useAct } from '@/hooks/use-act'
import { docPath } from '@/lib/api'
import { humanize, isLive, nodeLabel, PROGRESS_ORDER, statusColor } from '@/lib/status'
import type { ChangeState, Node } from '@/lib/types'
import { StatusDot } from '../status'
import { useNav } from './nav'
import { TaskGraph } from './task-graph'

const LATE_STAGES = ['integration', 'release', 'H-3', 'retrospective']

/** The stages in order, the build tasks as one step, each opening its node. */
function Pipeline({ nodes }: { nodes: Node[] }) {
  const nav = useNav()
  const builds = nodes.filter((n) => n.kind === 'build')
  const stages = nodes.filter((n) => n.kind !== 'build')
  const accepted = builds.filter((b) => b.status === 'ACCEPTED').length
  const buildStep = builds.length > 0 && (
    <Badge key="build" variant="outline" className="h-7 gap-1.5 px-2.5">
      <StatusDot status={accepted === builds.length ? 'ACCEPTED' : 'RUNNING'} />
      build {accepted}/{builds.length}
    </Badge>
  )
  const items: React.ReactNode[] = []
  let placed = false
  for (const n of stages) {
    if (!placed && LATE_STAGES.includes(n.id) && buildStep) {
      items.push(buildStep)
      placed = true
    }
    items.push(
      <Button
        key={n.id}
        variant="outline"
        size="sm"
        className="gap-1.5"
        onClick={() => nav.openNode(n.id)}
      >
        <StatusDot status={n.status} />
        {nodeLabel(n)}
      </Button>,
    )
  }
  if (!placed && buildStep) items.push(buildStep)
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {items.flatMap((it, i) =>
        i
          ? [
              <span key={`a${i}`} className="text-muted-foreground" aria-hidden>
                →
              </span>,
              it,
            ]
          : [it],
      )}
    </div>
  )
}

function ProgressBar({ state }: { state: ChangeState }) {
  const counts = new Map<string, number>()
  state.nodes.forEach((n) => counts.set(n.status, (counts.get(n.status) ?? 0) + 1))
  const total = state.nodes.length || 1
  const shown = PROGRESS_ORDER.filter((s) => counts.get(s))
  const live = state.runs.filter(isLive).length
  return (
    <div className="space-y-1.5">
      <div className="flex h-2 overflow-hidden rounded-full bg-muted">
        {shown.map((s) => (
          <div
            key={s}
            title={`${s}: ${counts.get(s)}`}
            style={{
              width: `${(100 * (counts.get(s) ?? 0)) / total}%`,
              backgroundColor: statusColor(s),
            }}
          />
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        {counts.get('ACCEPTED') ?? 0}/{state.nodes.length} nodes accepted
        {shown.map((s) => ` · ${humanize(s)} ${counts.get(s)}`).join('')} · {state.runs.length}{' '}
        run(s), {live} live
      </p>
    </div>
  )
}

/** Once the change has ended: its retrospective (automatic or asked for) and its report. */
function RetrospectiveCard({ state }: { state: ChangeState }) {
  const nav = useNav()
  const act = useAct(nav.cid)
  const [focus, setFocus] = useState('')
  const retro = state.nodes.find((n) => n.kind === 'retrospective')
  return (
    <Card>
      <CardHeader>
        <CardTitle>Retrospective</CardTitle>
        <CardDescription>
          An agent studies the change's logs, reflections and reviews and proposes improvements to
          the souls, skills and process.
        </CardDescription>
        {retro && (
          <CardAction>
            <Button
              variant="ghost"
              size="sm"
              className="gap-1.5"
              onClick={() => nav.openNode(retro.id)}
            >
              <StatusDot status={retro.status} />
              {humanize(retro.status)} · cycle {retro.cycle}
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center">
        {retro?.status === 'ACCEPTED' && (
          <Button
            variant="link"
            className="px-0"
            onClick={() => nav.openDoc(docPath(nav.cid, 'retro/report.md'))}
          >
            retro/report.md
          </Button>
        )}
        <Input
          value={focus}
          onChange={(e) => setFocus(e.target.value)}
          placeholder="Focus (optional): what to look at"
          className="sm:max-w-md"
        />
        <Button
          variant="secondary"
          onClick={async () => {
            if (await act.change('retrospective', { focus })) setFocus('')
          }}
        >
          {retro ? 'Run another retrospective' : 'Run retrospective'}
        </Button>
      </CardContent>
    </Card>
  )
}

export function OverviewTab({ state, selected }: { state: ChangeState; selected: string | null }) {
  const nav = useNav()
  const ended = state.change && state.change.status !== 'ACTIVE'
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Workflow</CardTitle>
          <CardDescription>{state.change?.goal}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Pipeline nodes={state.nodes} />
          <ProgressBar state={state} />
        </CardContent>
      </Card>
      {ended && <RetrospectiveCard state={state} />}
      <Card>
        <CardHeader>
          <CardTitle>Task graph</CardTitle>
          <CardDescription>Select a node for its details, decisions and runs.</CardDescription>
        </CardHeader>
        <CardContent>
          <TaskGraph
            nodes={state.nodes}
            runs={state.runs}
            stages={state.workflow?.stages}
            selected={selected}
            onSelect={nav.openNode}
          />
        </CardContent>
      </Card>
    </div>
  )
}
