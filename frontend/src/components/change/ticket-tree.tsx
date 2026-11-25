import { ChevronDown, ChevronRight } from 'lucide-react'
import { useState } from 'react'
import { descendantIds, isOpen, reconcile, toggle, visibleRows } from '@/lib/expansion'
import { isLive, kindOf, ticketState } from '@/lib/status'
import { treeRows } from '@/lib/tickets'
import type { ChangeState } from '@/lib/types'
import { cn } from '@/lib/utils'
import { StatusDot } from '../status'
import { useNav } from './nav'

const pillBase =
  'inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-4xl border border-transparent px-2 py-0.5 text-xs font-medium whitespace-nowrap'
const pillVariant = {
  outline: 'border-border font-normal text-foreground',
  secondary: 'bg-secondary text-secondary-foreground',
  destructive: 'bg-destructive/10 text-destructive dark:bg-destructive/20',
}

/** A badge look-alike without the hover styles of ui/badge: a row's highlight belongs to its li. */
function Pill({
  variant,
  className,
  ...props
}: { variant: keyof typeof pillVariant } & React.ComponentProps<'span'>) {
  return <span className={cn(pillBase, pillVariant[variant], className)} {...props} />
}

/** The change's tickets as a tree, in the order they run: each ticket's subtask groups (a group
 * of several runs in parallel), then its criteria. Only rows with something
 * underneath have a toggle (it expands or collapses them, the automatic rules of lib/expansion
 * apply when a status changes); every row always shows its goal, and its open button opens the
 * ticket. The hover and selection highlight sits on the whole row. */
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
        const all = descendantIds(state.tickets, t.id)
        const expandable = all.length > 0
        const open = expandable && isOpen(cur, t.id)
        const hidden = expandable && !open ? all : []
        const hiddenSigning = hidden.some((id) => Boolean(state.signoffs?.[id]))
        const hiddenWaiting = hidden.some((id) => byId.get(id)?.status === 'waiting')
        const containsSelected = selected !== null && hidden.includes(selected)
        return (
          <li
            key={t.id}
            data-ticket={t.id}
            data-expanded={expandable ? String(open) : undefined}
            data-contains-selected={containsSelected ? 'true' : undefined}
            className={cn(
              'flex items-center rounded-md pr-2 hover:bg-muted',
              selected === t.id && 'bg-muted',
            )}
            style={{ paddingLeft: `${0.5 + depth * 1.25}rem` }}
          >
            <span
              data-part="toggle-slot"
              className="flex size-5 shrink-0 items-center justify-center"
            >
              {expandable && (
                <button
                  type="button"
                  aria-expanded={open}
                  aria-label={`${open ? 'Collapse' : 'Expand'} ${t.id}`}
                  onClick={() => setExp((e) => toggle(e, t.id))}
                  className="rounded-md text-muted-foreground"
                >
                  {open ? (
                    <ChevronDown className="size-4" aria-hidden="true" />
                  ) : (
                    <ChevronRight className="size-4" aria-hidden="true" />
                  )}
                </button>
              )}
            </span>
            <button
              type="button"
              onClick={() => nav.openTicket(t.id)}
              aria-current={selected === t.id ? 'true' : undefined}
              className={cn(
                'flex min-w-0 flex-1 items-center gap-2 rounded-md py-1 pr-2 pl-0 text-left text-sm',
                t.status === 'cancelled' && 'text-muted-foreground line-through',
              )}
            >
              <span
                data-part="status-slot"
                className="flex size-5 shrink-0 items-center justify-center"
              >
                <StatusDot status={t.status} />
              </span>
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
              <Pill variant="outline">{kindOf(t)}</Pill>
              <span data-part="goal" className="min-w-0 flex-1 text-muted-foreground">
                {t.goal}
              </span>
              {hidden.length > 0 && (
                <span data-part="hidden-count" className="text-xs text-muted-foreground">
                  +{hidden.length}
                </span>
              )}
              {t.attempt > 1 && (
                <Pill variant="secondary" title="attempts used, of its limit">
                  {t.attempt}/{t.limit}
                </Pill>
              )}
              {(signing || hiddenSigning) && <Pill variant="destructive">sign-off due</Pill>}
              {(t.status === 'waiting' || hiddenWaiting) && (
                <Pill variant="destructive">{waitingLabel(t.id)}</Pill>
              )}
              <span className="shrink-0 text-xs text-muted-foreground">{ticketState(t, runs)}</span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}
