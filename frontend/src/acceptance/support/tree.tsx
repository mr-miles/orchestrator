import { act, render, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TicketTree } from '@/components/change/ticket-tree'
import { NavContext, type Nav } from '@/components/change/nav'
import { TooltipProvider } from '@/components/ui/tooltip'
import { stateAtH1, ticket } from '@/test/fakes'
import type { ChangeState, Ticket, TicketStatus } from '@/lib/types'

export interface Spec {
  parent?: string | null
  status?: TicketStatus
  subtasks?: string[][]
  criteria?: string[]
  goal?: string
  type?: string
  attempt?: number
}

/** A ticket for the tree (a leaf unless it has ``subtasks`` / ``criteria``). */
export function tk(id: string, s: Spec = {}): Ticket {
  return ticket({
    id,
    type: s.type ?? 'agent:implementer',
    parent: s.parent ?? null,
    status: s.status ?? 'pending',
    subtasks: s.subtasks ?? [],
    criteria: s.criteria ?? [],
    goal: s.goal ?? `goal of ${id}`,
    attempt: s.attempt ?? 0,
  })
}

/**
 * root
 *  ├─ A (subtasks)        ─ A1 (leaf)
 *  └─ B (leaf)
 * with the statuses given for A1 and B (root and A are 'subtasks').
 */
export function sample(a1: TicketStatus, b: TicketStatus = 'failed'): Ticket[] {
  return [
    tk('root', { status: 'subtasks', subtasks: [['A'], ['B']] }),
    tk('A', { parent: 'root', status: 'subtasks', subtasks: [['A1']] }),
    tk('A1', { parent: 'A', status: a1 }),
    tk('B', { parent: 'root', status: b }),
  ]
}

export interface Opts {
  selected?: string | null
  signoffs?: string[]
}

function stateOf(tickets: Ticket[], o: Opts): ChangeState {
  const base = stateAtH1()
  const signoffs: NonNullable<ChangeState['signoffs']> = {}
  for (const id of o.signoffs ?? []) signoffs[id] = { subjects: {}, attempt: 1 }
  return { ...base, tickets, runs: [], signoffs }
}

/** Render the tree with a recording ``openTicket``; ``update`` re-renders it with new tickets. */
export function mount(tickets: Ticket[], o: Opts = {}) {
  const opened: string[] = []
  const nav = {
    cid: 'c1',
    tab: 'overview',
    setTab: () => {},
    openTicket: (id: string) => void opened.push(id),
    openLog: () => {},
    openDoc: () => {},
    openDiff: () => {},
  } as Nav
  const tree = (ts: Ticket[], op: Opts) => (
    <TooltipProvider>
      <NavContext.Provider value={nav}>
        <TicketTree state={stateOf(ts, op)} selected={op.selected ?? null} />
      </NavContext.Provider>
    </TooltipProvider>
  )
  const r = render(tree(tickets, o))
  const user = userEvent.setup()
  const list = () => within(r.getByRole('list', { name: 'Tickets' }))
  return {
    opened,
    user,
    ids: () =>
      [...r.container.querySelectorAll('li[data-ticket]')].map((e) =>
        e.getAttribute('data-ticket'),
      ),
    row: (id: string) => r.container.querySelector(`li[data-ticket="${id}"]`) as HTMLElement | null,
    toggle: (id: string, open: boolean) =>
      list().queryByRole('button', { name: `${open ? 'Collapse' : 'Expand'} ${id}` }),
    click: async (id: string) => {
      const row = r.container.querySelector(`li[data-ticket="${id}"]`) as HTMLElement
      const t =
        within(row).queryByRole('button', { name: `Collapse ${id}` }) ??
        within(row).getByRole('button', { name: `Expand ${id}` })
      await user.click(t)
    },
    /** The row's open button (the one that is not the toggle). */
    openButton: (id: string) => {
      const row = r.container.querySelector(`li[data-ticket="${id}"]`) as HTMLElement
      return [...row.querySelectorAll('button')].find(
        (b) => !b.hasAttribute('aria-expanded'),
      ) as HTMLButtonElement
    },
    update: (ts: Ticket[], op: Opts = o) => act(() => r.rerender(tree(ts, op))),
  }
}

export type Mounted = ReturnType<typeof mount>
export const expanded = (m: Mounted, id: string) => m.row(id)?.getAttribute('data-expanded')
export const hasGoal = (m: Mounted, id: string) =>
  m.row(id)?.querySelector('[data-part="goal"]') != null
