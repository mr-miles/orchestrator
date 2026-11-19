import { PlusIcon } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardAction, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { CHANGE_DOT, CHANGE_STATUSES, humanize } from '@/lib/status'
import type { AttentionItem, ChangeSummary, Workspace } from '@/lib/types'
import { AwayDigest } from '../away-digest'
import { cn } from '@/lib/utils'
import { StatusDot } from '../status'
import { ChangesTable } from './changes-table'

type View = 'workspace' | 'all'
type Filter = ChangeSummary['status'] | 'needs_you' | null

const VIEW_KEY = 'work-view'

/** Every change across all workspaces: counted by status (each count filters the table), then a
 * table by workspace or all together. */
export function WorkPage({
  changes,
  workspaces,
  defaultWorkspace,
  onNew,
  onOpenItem,
}: {
  changes: ChangeSummary[]
  workspaces: Workspace[]
  defaultWorkspace: string
  onNew: () => void
  onOpenItem: (i: AttentionItem) => void
}) {
  const [view, setView] = useState<View>(() =>
    localStorage.getItem(VIEW_KEY) === 'all' ? 'all' : 'workspace',
  )
  const [filter, setFilter] = useState<Filter>(null)
  const pick = (v: View) => {
    localStorage.setItem(VIEW_KEY, v)
    setView(v)
  }
  const needs = changes.filter((c) => (c.needs_you ?? 0) > 0)
  const tiles: { id: Exclude<Filter, null>; label: string; count: number; dot: string }[] = [
    { id: 'needs_you', label: 'Need you', count: needs.length, dot: 'AWAITING_HUMAN' },
    ...CHANGE_STATUSES.map((s) => ({
      id: s,
      label: humanize(s),
      count: changes.filter((c) => c.status === s).length,
      dot: CHANGE_DOT[s],
    })),
  ]
  const shown =
    filter === 'needs_you' ? needs : filter ? changes.filter((c) => c.status === filter) : changes
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="mr-auto">
          <h1 className="font-heading text-xl font-semibold">Work</h1>
          <p className="text-sm text-muted-foreground">
            Every change across {workspaces.length === 1 ? 'the workspace' : 'all workspaces'}.
          </p>
        </div>
        <Button onClick={onNew}>
          <PlusIcon data-icon="inline-start" />
          New change
        </Button>
      </div>
      <AwayDigest onOpen={onOpenItem} />
      <div
        className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5"
        role="group"
        aria-label="Changes by status"
      >
        {tiles.map((t) => (
          <button
            key={t.id}
            type="button"
            aria-pressed={filter === t.id}
            onClick={() => setFilter(filter === t.id ? null : t.id)}
            className={cn(
              'flex flex-col gap-1 rounded-xl bg-card p-3 text-left ring-1 ring-foreground/10 transition hover:ring-foreground/30',
              filter === t.id && 'ring-2 ring-primary',
            )}
          >
            <span className="flex items-center gap-1.5 text-sm text-muted-foreground capitalize">
              <StatusDot status={t.dot} />
              {t.label}
            </span>
            <span className="font-heading text-2xl font-semibold tabular-nums">{t.count}</span>
          </button>
        ))}
      </div>
      <Card>
        <CardHeader>
          <CardTitle>
            {filter ? `${tiles.find((t) => t.id === filter)?.label} changes` : 'Changes'}
          </CardTitle>
          <CardDescription>
            {shown.length} of {changes.length}
            {filter && (
              <Button variant="link" className="ml-2 h-auto p-0" onClick={() => setFilter(null)}>
                Show all
              </Button>
            )}
          </CardDescription>
          <CardAction>
            <div role="group" aria-label="View" className="inline-flex rounded-md border p-0.5">
              {(
                [
                  ['workspace', 'By workspace'],
                  ['all', 'All changes'],
                ] as const
              ).map(([v, label]) => (
                <Button
                  key={v}
                  size="sm"
                  variant={view === v ? 'secondary' : 'ghost'}
                  aria-pressed={view === v}
                  onClick={() => pick(v)}
                >
                  {label}
                </Button>
              ))}
            </div>
          </CardAction>
        </CardHeader>
        <div className="px-4 pb-4">
          <ChangesTable
            changes={shown}
            workspaces={workspaces}
            defaultWorkspace={defaultWorkspace}
            grouped={view === 'workspace'}
            empty={changes.length ? 'No change matches.' : 'No changes yet: start one.'}
          />
        </div>
      </Card>
    </div>
  )
}
