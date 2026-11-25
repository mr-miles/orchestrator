import { afterEach, describe, expect, test } from 'bun:test'
import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TooltipProvider } from '@/components/ui/tooltip'
import { stateAtH1, ticket } from '@/test/fakes'
import type { Ticket, TicketStatus } from '@/lib/types'
import { NavContext, type Nav } from './nav'
import { TicketTree } from './ticket-tree'

afterEach(cleanup)

const tk = (id: string, status: TicketStatus, o: Partial<Ticket> = {}): Ticket =>
  ticket({ id, type: 'agent:implementer', status, goal: `goal of ${id}`, ...o })

/** root > A > A1, and root > B. */
const tree = (a1: TicketStatus, b: TicketStatus = 'failed'): Ticket[] => [
  tk('root', 'subtasks', { subtasks: [['A'], ['B']] }),
  tk('A', 'subtasks', { parent: 'root', subtasks: [['A1']] }),
  tk('A1', a1, { parent: 'A' }),
  tk('B', b, { parent: 'root' }),
]

function mount(tickets: Ticket[], o: { selected?: string; signoffs?: string[] } = {}) {
  const opened: string[] = []
  const nav: Nav = {
    cid: 'c1',
    tab: 'overview',
    setTab: () => {},
    openTicket: (id) => void opened.push(id),
    openLog: () => {},
    openDoc: () => {},
    openDiff: () => {},
  }
  const ui = (ts: Ticket[]) => {
    const signoffs: Record<string, { subjects: Record<string, never>; attempt: number }> = {}
    for (const id of o.signoffs ?? []) signoffs[id] = { subjects: {}, attempt: 1 }
    return (
      <TooltipProvider>
        <NavContext.Provider value={nav}>
          <TicketTree
            state={{ ...stateAtH1(), tickets: ts, runs: [], signoffs }}
            selected={o.selected ?? null}
          />
        </NavContext.Provider>
      </TooltipProvider>
    )
  }
  const r = render(ui(tickets))
  const ids = () =>
    [...r.container.querySelectorAll('li[data-ticket]')].map((e) => e.getAttribute('data-ticket'))
  const row = (id: string) => r.container.querySelector(`li[data-ticket="${id}"]`) as HTMLElement
  return { opened, ids, row, update: (ts: Ticket[]) => r.rerender(ui(ts)) }
}

describe('TicketTree expansion', () => {
  test('an active leaf and its ancestors are open; others follow their status', () => {
    const m = mount(tree('working'))
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
    expect(screen.getByRole('button', { name: 'Collapse A' }).getAttribute('aria-expanded')).toBe(
      'true',
    )
  })

  test('a pending parent starts collapsed and hides its rows', () => {
    const ts = tree('pending')
    ts[1] = tk('A', 'pending', { parent: 'root', subtasks: [['A1']] })
    const m = mount(ts)
    expect(m.ids()).toEqual(['root', 'A', 'B'])
    expect(m.row('A').getAttribute('data-expanded')).toBe('false')
    expect(screen.getByRole('button', { name: 'Expand A' }).getAttribute('aria-expanded')).toBe(
      'false',
    )
  })

  test('the toggle expands and collapses without opening the ticket', async () => {
    const m = mount(tree('working'))
    await userEvent.click(screen.getByRole('button', { name: 'Collapse A' }))
    expect(m.ids()).toEqual(['root', 'A', 'B'])
    await userEvent.click(screen.getByRole('button', { name: 'Expand A' }))
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
    expect(m.opened).toEqual([])
    await userEvent.click(within(m.row('A')).getByText('A', { selector: '.font-mono' }))
    expect(m.opened).toEqual(['A'])
  })

  test('every row shows its goal, collapsed or expanded', async () => {
    const m = mount(tree('working'))
    await userEvent.click(screen.getByRole('button', { name: 'Collapse A' }))
    expect(m.row('A').getAttribute('data-expanded')).toBe('false')
    expect(m.row('A').querySelector('[data-part="goal"]')?.textContent).toBe('goal of A')
    expect(m.row('root').querySelector('[data-part="goal"]')?.textContent).toBe('goal of root')
    expect(m.row('B').querySelector('[data-part="goal"]')?.textContent).toBe('goal of B')
  })

  test('leaf rows have no toggle, aria-expanded or data-expanded; parents do', () => {
    const m = mount(tree('working'))
    for (const id of ['A1', 'B']) {
      expect(screen.queryByRole('button', { name: `Collapse ${id}` })).toBeNull()
      expect(screen.queryByRole('button', { name: `Expand ${id}` })).toBeNull()
      expect(m.row(id).querySelector('[aria-expanded]')).toBeNull()
      expect(m.row(id).hasAttribute('data-expanded')).toBe(false)
    }
    for (const id of ['root', 'A']) {
      expect(m.row(id).querySelector('button[aria-expanded]')).toBeTruthy()
      expect(m.row(id).hasAttribute('data-expanded')).toBe(true)
    }
  })

  test('a completed leaf never hides anything or shows a count', () => {
    const m = mount(tree('working'))
    m.update(tree('passed'))
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
    expect(m.row('A1').hasAttribute('data-expanded')).toBe(false)
    expect(m.row('A1').querySelector('[data-part="hidden-count"]')).toBeNull()
  })

  test('the highlight is on the li only, and the slots are size-5 on every row', () => {
    const m = mount(tree('working'), { selected: 'B' })
    for (const id of m.ids() as string[]) {
      const li = m.row(id)
      expect(li.className).toContain('hover:bg-muted')
      for (const el of li.querySelectorAll('*'))
        expect(el.getAttribute('class') ?? '').not.toContain('hover:bg-')
      for (const part of ['toggle-slot', 'status-slot']) {
        const slots = li.querySelectorAll(`[data-part="${part}"]`)
        expect(slots.length).toBe(1)
        expect(slots[0]!.className).toContain('size-5')
      }
    }
    expect(m.row('B').className).toContain('bg-muted')
  })

  test('a leaf that becomes active opens itself and its ancestors', async () => {
    const m = mount(tree('pending'))
    await userEvent.click(screen.getByRole('button', { name: 'Collapse A' }))
    await userEvent.click(screen.getByRole('button', { name: 'Collapse root' }))
    expect(m.ids()).toEqual(['root'])
    m.update(tree('working'))
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
  })

  test('a collapsed row shows a count and the hidden attention badges', async () => {
    const ts = tree('working')
    ts[2] = tk('A1', 'waiting', { parent: 'A' })
    const m = mount(ts, { signoffs: ['A1'], selected: 'A1' })
    await userEvent.click(screen.getByRole('button', { name: 'Collapse A' }))
    const a = m.row('A')
    expect(a.querySelector('[data-part="hidden-count"]')?.textContent).toBe('+1')
    expect(within(a).getByText('sign-off due')).toBeTruthy()
    expect(within(a).getByText('waiting')).toBeTruthy()
    expect(a.getAttribute('data-contains-selected')).toBe('true')
    expect(m.row('root').getAttribute('data-contains-selected')).toBeNull()
  })

  test('selected row keeps aria-current on its open button', () => {
    const m = mount(tree('working'), { selected: 'B' })
    expect(m.row('B').querySelector('[aria-current="true"]')).toBeTruthy()
  })
})
