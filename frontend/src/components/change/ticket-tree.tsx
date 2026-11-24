import { Badge } from '@/components/ui/badge'
import { isLive, kindOf, ticketState } from '@/lib/status'
import { treeRows } from '@/lib/tickets'
import type { ChangeState } from '@/lib/types'
import { cn } from '@/lib/utils'
import { StatusDot } from '../status'
import { useNav } from './nav'

/** The change's tickets as a tree, in the order they run: each ticket's subtask groups (a group
 * of several runs in parallel), then its criteria. A row opens the ticket. */
export function TicketTree({ state, selected }: { state: ChangeState; selected: string | null }) {
  const nav = useNav()
  const live = state.runs.filter(isLive)
  const proposals = state.change?.proposals ?? {}
  return (
    <ol className="flex flex-col" aria-label="Tickets">
      {treeRows(state.tickets).map(({ ticket: t, depth, group, parallel }) => {
        const signing = Boolean(state.signoffs?.[t.id])
        const runs = live.filter((r) => r.ticket === t.id)
        return (
          <li key={t.id}>
            <button
              type="button"
              onClick={() => nav.openTicket(t.id)}
              aria-current={selected === t.id ? 'true' : undefined}
              className={cn(
                'flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-sm hover:bg-muted',
                selected === t.id && 'bg-muted',
                t.status === 'cancelled' && 'text-muted-foreground line-through',
              )}
              style={{ paddingLeft: `${0.5 + depth * 1.25}rem` }}
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
              <span className="min-w-0 flex-1 truncate text-muted-foreground">{t.goal}</span>
              {t.attempt > 1 && (
                <Badge variant="secondary" title="attempts used, of its limit">
                  {t.attempt}/{t.limit}
                </Badge>
              )}
              {signing && <Badge variant="destructive">sign-off due</Badge>}
              {t.status === 'waiting' && (
                <Badge variant="destructive">{proposals[t.id] ? 'proposal' : 'waiting'}</Badge>
              )}
              <span className="shrink-0 text-xs text-muted-foreground">{ticketState(t, runs)}</span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}
