import { ChevronDown, ChevronRight } from 'lucide-react'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { descendantIds, isOpen, reconcile, toggle, visibleRows } from '@/lib/expansion'
import { isLive, kindOf, ticketState } from '@/lib/status'
import { treeRows } from '@/lib/tickets'
import type { ChangeState } from '@/lib/types'
import { cn } from '@/lib/utils'
import { StatusDot } from '../status'
import { useNav } from './nav'

/** The change's tickets as a tree, in the order they run: each ticket's subtask groups (a group
 * of several runs in parallel), then its criteria. A row's toggle expands or collapses it (the
 * automatic rules of lib/expansion apply when a status changes); its open button opens the ticket. */
export function TicketTree({ state, selected }: { state: ChangeState; selected: string | null }) {
  const nav = useNav()
  const live = state.runs.filter(isLive)
  const proposals = state.change?.proposals ?? {}
  const [exp, setExp] = useState(() => reconcile(state.tickets, null))
  const cur = reconcile(state.tickets, exp)
  if (cur !== exp) setExp(cur)
  const byId = new Map(state.tickets.map((t) => [t.id, t]))
  const waitingLabel = (id: string) => (proposals[id] ? 'proposal' : 'waiting')
  return (
    <ol className="flex flex-col" aria-label="Tickets">
      {visibleRows(treeRows(state.tickets), cur).map(({ ticket: t, depth, group, parallel }) => {
        const signing = Boolean(state.signoffs?.[t.id])
        const runs = live.filter((r) => r.ticket === t.id)
        const open = isOpen(cur, t.id)
        const hidden = open ? [] : descendantIds(state.tickets, t.id)
        const hiddenSigning = hidden.some((id) => Boolean(state.signoffs?.[id]))
        const hiddenWaiting = hidden.some((id) => byId.get(id)?.status === 'waiting')
        const containsSelected = selected !== null && hidden.includes(selected)
        return (
          <li
            key={t.id}
            data-ticket={t.id}
            data-expanded={String(open)}
            data-contains-selected={containsSelected ? 'true' : undefined}
            className="flex items-start gap-1"
            style={{ paddingLeft: `${0.5 + depth * 1.25}rem` }}
          >
            <button
              type="button"
              aria-expanded={open}
              aria-label={`${open ? 'Collapse' : 'Expand'} ${t.id}`}
              onClick={() => setExp((e) => toggle(e, t.id))}
              className="mt-0.5 shrink-0 rounded-md p-1 text-muted-foreground hover:bg-muted"
            >
              {open ? (
                <ChevronDown className="size-4" aria-hidden="true" />
              ) : (
                <ChevronRight className="size-4" aria-hidden="true" />
              )}
            </button>
            <button
              type="button"
              onClick={() => nav.openTicket(t.id)}
              aria-current={selected === t.id ? 'true' : undefined}
              className={cn(
                'flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1 text-left text-sm hover:bg-muted',
                selected === t.id && 'bg-muted',
                t.status === 'cancelled' && 'text-muted-foreground line-through',
              )}
            >
              <StatusDot status={t.status} />
              {t.role === 'criterion' ? (
                <span className="text-muted-foreground" aria-label="criterion">
                  ✓
                </span>
              ) : (
                parallel && (
                  <span className="text-muted-foreground" title={`group ${(group ?? 0) + 1}`}>
                    ∥
                  </span>
                )
              )}
              <span className="font-mono">{t.id}</span>
              <Badge variant="outline" className="font-normal">
                {kindOf(t)}
              </Badge>
              {open ? (
                <span data-part="goal" className="min-w-0 flex-1 text-muted-foreground">
                  {t.goal}
                </span>
              ) : (
                <span className="flex-1" />
              )}
              {hidden.length > 0 && (
                <span data-part="hidden-count" className="text-xs text-muted-foreground">
                  +{hidden.length}
                </span>
              )}
              {t.attempt > 1 && (
                <Badge variant="secondary" title="attempts used, of its limit">
                  {t.attempt}/{t.limit}
                </Badge>
              )}
              {(signing || hiddenSigning) && <Badge variant="destructive">sign-off due</Badge>}
              {(t.status === 'waiting' || hiddenWaiting) && (
                <Badge variant="destructive">{waitingLabel(t.id)}</Badge>
              )}
              <span className="shrink-0 text-xs text-muted-foreground">{ticketState(t, runs)}</span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}
