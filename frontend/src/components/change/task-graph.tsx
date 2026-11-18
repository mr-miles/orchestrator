import { useMemo } from 'react'
import { layout } from '@/lib/layout'
import { isLive, nodeState, statusColor, STATUSES, humanize } from '@/lib/status'
import type { Node, Run } from '@/lib/types'
import { cn } from '@/lib/utils'
import { StatusDot } from '../status'

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

/** The change's task graph as a layered DAG; a node opens its details. */
export function TaskGraph({
  nodes,
  runs,
  selected,
  onSelect,
}: {
  nodes: Node[]
  runs: Run[]
  selected?: string | null
  onSelect: (id: string) => void
}) {
  const g = useMemo(() => layout(nodes), [nodes])
  const byId = new Map(nodes.map((n) => [n.id, n]))
  return (
    <div className="space-y-2">
      <div className="overflow-auto rounded-lg border bg-muted/20">
        <svg
          width={g.width}
          height={g.height}
          role="img"
          aria-label="Task graph"
          className="text-[11px]"
        >
          {g.edges.map((e) => (
            <path
              key={`${e.from}->${e.to}`}
              d={e.path}
              fill="none"
              strokeWidth={1.4}
              className={cn(
                byId.get(e.from)?.status === 'ACCEPTED'
                  ? 'stroke-green-600/60'
                  : 'stroke-muted-foreground/40',
              )}
            />
          ))}
          {nodes.map((n) => {
            const p = g.pos[n.id]
            if (!p) return null
            const c = statusColor(n.status)
            const live = runs.filter((r) => r.node_id === n.id && isLive(r))
            const sub = [
              nodeState(n, live),
              n.task?.scaffold && 'scaffold',
              n.kind === 'build' && (n.round ?? 1) > 1 && `round ${n.round}`,
              n.cycle > 1 && `cycle ${n.cycle}`,
              live.length > 0 && live.map((r) => r.role).join('+'),
            ]
              .filter(Boolean)
              .join(' · ')
            const title = n.kind === 'human' ? `${n.id} · ${n.title}` : n.title
            const busy = ['RUNNING', 'VERIFYING', 'REVIEWING', 'MERGING'].includes(n.status)
            return (
              <g
                key={n.id}
                role="button"
                tabIndex={0}
                aria-label={`${title}: ${sub}`}
                className="cursor-pointer outline-none [&:focus-visible>rect:first-child]:stroke-ring"
                onClick={() => onSelect(n.id)}
                onKeyDown={(ev) => {
                  if (ev.key === 'Enter' || ev.key === ' ') onSelect(n.id)
                }}
              >
                <rect
                  x={p.x}
                  y={p.y}
                  width={g.W}
                  height={g.H}
                  rx={7}
                  fill={c}
                  fillOpacity={0.12}
                  stroke={selected === n.id ? 'var(--foreground)' : c}
                  strokeWidth={selected === n.id ? 2.5 : 1.5}
                  className={cn(busy && 'animate-pulse')}
                />
                <rect x={p.x} y={p.y} width={5} height={g.H} rx={2} fill={c} />
                <text x={p.x + 12} y={p.y + 18} className="fill-foreground font-semibold">
                  {clip(title, 26)}
                </text>
                <text x={p.x + 12} y={p.y + 35} fill={c}>
                  {clip(sub, 30)}
                </text>
                <title>{`${n.title}\n${n.status}`}</title>
              </g>
            )
          })}
        </svg>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {STATUSES.map((s) => (
          <span key={s} className="inline-flex items-center gap-1.5">
            <StatusDot status={s} />
            {humanize(s)}
          </span>
        ))}
      </div>
    </div>
  )
}
