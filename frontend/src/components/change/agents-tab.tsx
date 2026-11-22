import { ScrollTextIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { isLive, STAGE_TEXT } from '@/lib/status'
import type { A2ACard, Run } from '@/lib/types'
import { StageBadge, StatusBadge } from '../status'
import { AgentCards } from './a2a'
import { useNav } from './nav'

function LiveRun({ run }: { run: Run }) {
  const nav = useNav()
  const p = run.progress.at(-1)
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <span className="font-mono">{run.id}</span>
          {run.stage ? <StageBadge stage={run.stage} /> : <StatusBadge status={run.status} />}
          <span className="font-normal">{run.role}</span>
        </CardTitle>
        <CardDescription>
          <Button variant="link" className="h-auto p-0" onClick={() => nav.openNode(run.node_id)}>
            {run.node_id}
          </Button>{' '}
          · soul {run.soul} · sandbox <code className="font-mono">{run.sandbox}</code> · reports{' '}
          {run.attempts}
        </CardDescription>
        <CardAction>
          <Button variant="outline" size="sm" onClick={() => nav.openLog(run.id)}>
            <ScrollTextIcon data-icon="inline-start" />
            Log
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="text-sm">
        {p ? (
          <p>
            {p.percent != null && <b>{Math.round(p.percent)}% </b>}
            {p.message}
          </p>
        ) : (
          <p className="text-muted-foreground">{run.stage ? STAGE_TEXT[run.stage] : 'starting'}…</p>
        )}
      </CardContent>
    </Card>
  )
}

export function AgentsTab({ runs, cards = [] }: { runs: Run[]; cards?: A2ACard[] }) {
  const nav = useNav()
  const live = runs.filter(isLive)
  return (
    <div className="flex flex-col gap-4">
      {live.length ? (
        <div className="grid gap-3 lg:grid-cols-2">
          {live.map((r) => (
            <LiveRun key={r.id} run={r} />
          ))}
        </div>
      ) : (
        <Empty className="border">
          <EmptyHeader>
            <EmptyTitle>No agents running</EmptyTitle>
            <EmptyDescription>Runs appear here while their agents work.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
      <AgentCards cards={cards} />
      <Card>
        <CardHeader>
          <CardTitle>All runs</CardTitle>
          <CardDescription>Every agent run of this change, newest first.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Node</TableHead>
                <TableHead>Cycle</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Log</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {[...runs].reverse().map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="font-mono">{r.id}</TableCell>
                  <TableCell>{r.role}</TableCell>
                  <TableCell>
                    <Button
                      variant="link"
                      className="h-auto p-0"
                      onClick={() => nav.openNode(r.node_id)}
                    >
                      {r.node_id}
                    </Button>
                  </TableCell>
                  <TableCell>{r.cycle}</TableCell>
                  <TableCell>
                    <StatusBadge status={r.status} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={`Log of ${r.id}`}
                      onClick={() => nav.openLog(r.id)}
                    >
                      <ScrollTextIcon />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
