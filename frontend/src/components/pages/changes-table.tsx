import { Fragment } from 'react'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { changeHref, formatHash } from '@/lib/route'
import { CHANGE_DOT, humanize } from '@/lib/status'
import type { ChangeSummary, Workspace } from '@/lib/types'
import { Muted, StatusDot } from '../status'

function Row({ c, ws, showWorkspace }: { c: ChangeSummary; ws: string; showWorkspace: boolean }) {
  const href = changeHref(c, ws)
  return (
    <TableRow className="cursor-pointer" onClick={() => (location.hash = href)}>
      <TableCell className="font-mono text-xs">
        <a href={href} onClick={(e) => e.stopPropagation()} className="hover:underline">
          {c.id}
        </a>
      </TableCell>
      <TableCell className="max-w-md truncate" title={c.goal}>
        {c.goal}
      </TableCell>
      {showWorkspace && <TableCell className="text-xs">{c.workspace ?? ws}</TableCell>}
      <TableCell>
        <span className="inline-flex items-center gap-1.5 text-xs">
          <StatusDot status={CHANGE_DOT[c.status]} />
          {humanize(c.status)}
        </span>
      </TableCell>
      <TableCell className="text-xs">{c.tier ?? '—'}</TableCell>
      <TableCell className="text-xs">
        {c.nodes ? `${c.accepted}/${c.nodes} nodes` : '—'}
        {c.live_runs > 0 && <span className="text-muted-foreground"> · {c.live_runs} live</span>}
      </TableCell>
      <TableCell className="text-xs">
        {(c.needs_you ?? 0) > 0 && (
          <span className="font-medium text-destructive">{c.needs_you} need you</span>
        )}
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">{c.created_at.slice(0, 16)}</TableCell>
    </TableRow>
  )
}

/** Changes, newest first: flat, or grouped under their workspace. */
export function ChangesTable({
  changes,
  workspaces,
  defaultWorkspace,
  grouped,
  empty = 'No changes.',
}: {
  changes: ChangeSummary[]
  workspaces: Workspace[]
  /** the workspace of a change that does not say (a controller serves one) */
  defaultWorkspace: string
  grouped: boolean
  empty?: string
}) {
  const sorted = [...changes].sort((a, b) => b.created_at.localeCompare(a.created_at))
  const wsOf = (c: ChangeSummary) => c.workspace ?? defaultWorkspace
  const groups = grouped
    ? [
        ...workspaces.map((w) => ({ id: w.id, name: w.name || w.id })),
        ...[...new Set(sorted.map(wsOf))]
          .filter((id) => !workspaces.some((w) => w.id === id))
          .map((id) => ({ id, name: id })),
      ]
    : []
  const cols = grouped ? 7 : 8
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Change</TableHead>
          <TableHead>Goal</TableHead>
          {!grouped && <TableHead>Workspace</TableHead>}
          <TableHead>Status</TableHead>
          <TableHead>Tier</TableHead>
          <TableHead>Progress</TableHead>
          <TableHead>Needs you</TableHead>
          <TableHead>Created</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sorted.length === 0 && (
          <TableRow>
            <TableCell colSpan={cols}>
              <Muted>{empty}</Muted>
            </TableCell>
          </TableRow>
        )}
        {grouped
          ? groups.map((g) => {
              const rows = sorted.filter((c) => wsOf(c) === g.id)
              if (!rows.length) return null
              return (
                <Fragment key={g.id}>
                  <TableRow className="bg-muted/40 hover:bg-muted/40">
                    <TableCell colSpan={cols} className="py-1.5">
                      <a
                        href={formatHash({ page: 'workspace', ws: g.id, tab: 'changes' })}
                        className="text-sm font-medium hover:underline"
                      >
                        {g.name}
                      </a>
                      <span className="ml-2 text-xs text-muted-foreground">
                        {rows.length} change{rows.length === 1 ? '' : 's'}
                      </span>
                    </TableCell>
                  </TableRow>
                  {rows.map((c) => (
                    <Row key={c.id} c={c} ws={defaultWorkspace} showWorkspace={false} />
                  ))}
                </Fragment>
              )
            })
          : sorted.map((c) => <Row key={c.id} c={c} ws={defaultWorkspace} showWorkspace />)}
      </TableBody>
    </Table>
  )
}
