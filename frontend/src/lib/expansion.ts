import type { TreeRow } from './tickets'
import type { Ticket, TicketStatus } from './types'

/** Which tickets of the tree are drawn open: the status each had when last reconciled, and the
 * explicit open/closed choice (an id absent from ``open`` is open). */
export interface Expansion {
  status: Record<string, TicketStatus>
  open: Record<string, boolean>
}

/** The statuses of a ticket that is being worked on right now. */
export const ACTIVE_STATUSES: readonly TicketStatus[] = ['working', 'checking', 'merging']

/** A ticket with no subtasks and no criteria. */
export const isLeaf = (t: Ticket): boolean =>
  t.subtasks.flat().length === 0 && t.criteria.length === 0

export const isOpen = (e: Expansion, id: string): boolean => e.open[id] ?? true

/** Apply the automatic rules to the tickets whose status changed since ``prev``: passed and
 * pending collapse, then active leaves and their ancestors open. Returns ``prev`` itself when
 * no status changed; never mutates its inputs. */
export function reconcile(tickets: Ticket[], prev: Expansion | null): Expansion {
  const changed = tickets.filter((t) => prev?.status[t.id] !== t.status)
  if (prev && changed.length === 0) return prev
  const status = { ...prev?.status }
  const open = { ...prev?.open }
  for (const t of changed) {
    status[t.id] = t.status
    if (t.status === 'passed' || t.status === 'pending') open[t.id] = false
  }
  const byId = new Map(tickets.map((t) => [t.id, t]))
  for (const t of changed) {
    if (!isLeaf(t) || !ACTIVE_STATUSES.includes(t.status)) continue
    open[t.id] = true
    const seen = new Set([t.id])
    let p = t.parent == null ? undefined : byId.get(t.parent)
    while (p && !seen.has(p.id)) {
      seen.add(p.id)
      open[p.id] = true
      p = p.parent == null ? undefined : byId.get(p.parent)
    }
  }
  return { status, open }
}

/** Flip one ticket's open state, keeping the recorded statuses. */
export function toggle(e: Expansion, id: string): Expansion {
  return { status: e.status, open: { ...e.open, [id]: !isOpen(e, id) } }
}

/** The rows not hidden under a collapsed ancestor (rows are depth-first). */
export function visibleRows(rows: TreeRow[], e: Expansion): TreeRow[] {
  const out: TreeRow[] = []
  let hideBelow: number | null = null
  for (const r of rows) {
    if (hideBelow !== null) {
      if (r.depth > hideBelow) continue
      hideBelow = null
    }
    out.push(r)
    if (!isOpen(e, r.ticket.id)) hideBelow = r.depth
  }
  return out
}

/** Every ticket under ``id`` – subtasks of all groups, then criteria, recursively. */
export function descendantIds(tickets: Ticket[], id: string): string[] {
  const byId = new Map(tickets.map((t) => [t.id, t]))
  const seen = new Set([id])
  const out: string[] = []
  const walk = (t: Ticket | undefined) => {
    if (!t) return
    for (const c of [...t.subtasks.flat(), ...t.criteria]) {
      if (seen.has(c)) continue
      seen.add(c)
      out.push(c)
      walk(byId.get(c))
    }
  }
  walk(byId.get(id))
  return out
}
