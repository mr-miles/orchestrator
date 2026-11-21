import { useMemo } from 'react'
import { layout } from '@/lib/layout'
import { isLive, nodeState, statusColor, STATUSES, humanize } from '@/lib/status'
import type { Node, Run, Stage } from '@/lib/types'
import { cn } from '@/lib/utils'
import { StatusDot } from '../status'

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

// a stage's box: padding around its nodes, more for each stage level it contains, and a band
// at the top for its title
const PAD = 6
const PAD_LEVEL = 8
const LABEL = 15

interface StageBox {
  stage: Stage
  x: number
  y: number
  w: number
  h: number
}

/** The boxes of the stages that have nodes on the graph, outermost first. */
function stageBoxes(stages: Stage[], g: ReturnType<typeof layout>): StageBox[] {
  const out: StageBox[] = []
  for (const st of [...stages].sort((a, b) => a.depth - b.depth)) {
    const ps = st.nodes.map((id) => g.pos[id]).filter((p) => p !== undefined)
    if (!ps.length) continue
    const pad = PAD + PAD_LEVEL * st.level
    const x0 = Math.min(...ps.map((p) => p.x)) - pad
    const y0 = Math.min(...ps.map((p) => p.y)) - pad - LABEL * (st.level + 1)
    const x1 = Math.max(...ps.map((p) => p.x + g.W)) + pad
    const y1 = Math.max(...ps.map((p) => p.y + g.H)) + pad
    out.push({ stage: st, x: x0, y: y0, w: x1 - x0, h: y1 - y0 })
  }
  return out
}

const STAGE_STROKE: Record<string, string> = {
  done: 'stroke-green-600/50',
  running: 'stroke-foreground/30',
  pending: 'stroke-muted-foreground/30',
}

/** The change's task graph as a layered DAG inside boxes for its stages; a node opens its
 * details. */
export function TaskGraph({
  nodes,
  runs,
  stages = [],
  selected,
  onSelect,
}: {
  nodes: Node[]
  runs: Run[]
  stages?: Stage[]
  selected?: string | null
  onSelect: (id: string) => void
}) {
  const g = useMemo(() => layout(nodes), [nodes])
  const boxes = useMemo(() => stageBoxes(stages, g), [stages, g])
  // room around the drawing for the outermost boxes
  const top = Math.max(0, ...boxes.map((b) => -b.y + 4))
  const left = Math.max(0, ...boxes.map((b) => -b.x + 4))
  const right = Math.max(0, ...boxes.map((b) => b.x + b.w + 4 - g.width))
  const bottom = Math.max(0, ...boxes.map((b) => b.y + b.h + 4 - g.height))
  const byId = new Map(nodes.map((n) => [n.id, n]))
  return (
    <div className="space-y-2">
      <div className="overflow-auto rounded-lg border bg-muted/20">
        <svg
          width={g.width + left + right}
          height={g.height + top + bottom}
          role="img"
          aria-label="Task graph"
          className="text-[11px]"
        >
          <g transform={`translate(${left},${top})`}>
            {boxes.map((b) => (
              <g key={b.stage.id} aria-label={`Stage ${b.stage.title}: ${b.stage.status}`}>
                <rect
                  x={b.x}
                  y={b.y}
                  width={b.w}
                  height={b.h}
                  rx={10}
                  fill="none"
                  strokeWidth={1.2}
                  strokeDasharray={b.stage.status === 'pending' ? '4 3' : undefined}
                  className={STAGE_STROKE[b.stage.status] ?? STAGE_STROKE.pending}
                />
                <text
                  x={b.x + 8}
                  y={b.y + 11}
                  className="fill-muted-foreground text-[10px] font-medium"
                >
                  {b.stage.title}
                </text>
              </g>
            ))}
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
              const busy = ['RUNNING', 'VERIFYING', 'REVIEWING', 'DEFINING', 'MERGING'].includes(
                n.status,
              )
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
          </g>
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
