import type { ChangeState, Ticket } from './types'

/** One row of the ticket tree, in the order it is drawn: a ticket, how deep it sits, and where
 * (a subtask in group ``group`` of its parent – ``parallel`` if that group has others – or a
 * criterion). */
export interface TreeRow {
  ticket: Ticket
  depth: number
  group: number | null
  parallel: boolean
}

/** The tree from its root down: each ticket, then its subtask groups in order, then its
 * criteria in order – the order tickets run in. */
export function treeRows(tickets: Ticket[]): TreeRow[] {
  const byId = new Map(tickets.map((t) => [t.id, t]))
  const root = tickets.find((t) => t.parent == null)
  const out: TreeRow[] = []
  const visit = (t: Ticket, depth: number, group: number | null, parallel: boolean) => {
    out.push({ ticket: t, depth, group, parallel })
    t.subtasks.forEach((g, i) =>
      g.forEach((id) => {
        const c = byId.get(id)
        if (c) visit(c, depth + 1, i, g.length > 1)
      }),
    )
    t.criteria.forEach((id) => {
      const c = byId.get(id)
      if (c) visit(c, depth + 1, null, false)
    })
  }
  if (root) visit(root, 0, null, false)
  return out
}

/** The tickets a decision owner must act on: sign-offs waiting for them and tickets waiting on
 * a human. */
export function needsDecision(state: ChangeState): string[] {
  const waiting = state.tickets.filter((t) => t.status === 'waiting').map((t) => t.id)
  return [...new Set([...Object.keys(state.signoffs ?? {}), ...waiting])]
}

/** The tickets that do work of their own (grouping tickets have none): what progress counts. */
export const withWork = (tickets: Ticket[]) => tickets.filter((t) => t.type !== 'grouping')

/** A cheap fingerprint of the change's documents moving on: views that fetch documents refresh
 * on it. */
export function docsVersion(state: ChangeState): string {
  const tickets = state.tickets.map((t) => `${t.status}${t.attempt}`).join()
  return `${state.runs.length}:${tickets}:${Object.values(state.pointers ?? {}).join()}`
}

/** The documents of an output: one ``file:`` reference, or a list of them. */
export const refsOf = (v: string | string[] | undefined | null): string[] =>
  v == null ? [] : Array.isArray(v) ? v : [v]
