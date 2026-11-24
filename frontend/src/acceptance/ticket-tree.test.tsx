import { afterEach, describe, expect, test } from 'bun:test'
import { cleanup, within } from '@testing-library/react'
import { classes, expanded, hasGoal, mount, sample, tk } from './support/tree'

afterEach(cleanup)

const EXPAND = /Expand|Collapse/
const toggleButtons = (row: HTMLElement) => within(row).queryAllByRole('button', { name: EXPAND })

describe('AC-1 title always visible', () => {
  test('test_AC_1_goal_shown_on_every_row_expanded_or_collapsed', async () => {
    const m = mount(sample('failed'))
    for (const id of ['root', 'A', 'A1', 'B']) {
      expect(hasGoal(m, id)).toBe(true)
      expect(m.row(id)?.querySelector('[data-part="goal"]')?.textContent).toBe(`goal of ${id}`)
    }
    await m.click('A')
    expect(expanded(m, 'A')).toBe('false')
    expect(m.row('A')?.querySelector('[data-part="goal"]')?.textContent).toBe('goal of A')
    await m.click('root')
    expect(m.ids()).toEqual(['root'])
    expect(m.row('root')?.querySelector('[data-part="goal"]')?.textContent).toBe('goal of root')
  })

  test('test_AC_1_collapsed_row_keeps_id_kind_hidden_count_and_attention_badges', async () => {
    const ts = sample('waiting')
    ts[1] = tk('A', {
      parent: 'root',
      status: 'subtasks',
      subtasks: [['A1']],
      type: 'agent:planner',
    })
    const m = mount(ts, { signoffs: ['A1'] })
    await m.click('A')
    const a = m.row('A') as HTMLElement
    expect(expanded(m, 'A')).toBe('false')
    expect(a.textContent).toContain('A')
    expect(a.textContent).toContain('planner')
    expect(a.querySelector('[data-part="hidden-count"]')?.textContent).toContain('+1')
    expect(a.textContent).toContain('sign-off due')
    expect(a.textContent).toMatch(/waiting|proposal/)
    expect(hasGoal(m, 'A')).toBe(true)
  })

  test('test_AC_1_passed_and_pending_leaf_still_show_goal', () => {
    for (const s of ['passed', 'pending'] as const) {
      const m = mount(sample(s))
      expect(hasGoal(m, 'A1')).toBe(true)
      cleanup()
    }
    const ts = sample('failed')
    ts[1] = tk('A', { parent: 'root', status: 'passed', subtasks: [['A1']] })
    const m = mount(ts)
    expect(hasGoal(m, 'A')).toBe(true)
  })
})

describe('AC-2 toggle only where something is underneath', () => {
  test('test_AC_2_leaves_have_no_toggle_parents_do', () => {
    const m = mount(sample('failed'))
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
    for (const id of ['A1', 'B']) {
      const row = m.row(id) as HTMLElement
      expect(toggleButtons(row)).toHaveLength(0)
      expect(row.querySelector('[aria-expanded]')).toBeNull()
      expect(row.hasAttribute('data-expanded')).toBe(false)
    }
    for (const id of ['root', 'A']) {
      const row = m.row(id) as HTMLElement
      expect(toggleButtons(row)).toHaveLength(1)
      expect(row.querySelector('[aria-expanded]')).not.toBeNull()
      expect(row.hasAttribute('data-expanded')).toBe(true)
    }
  })

  test('test_AC_2_criteria_gates_and_stored_closed_leaves_have_no_toggle_or_count', () => {
    const ts = [
      tk('root', { status: 'subtasks', subtasks: [['provision'], ['T']], criteria: ['H-1'] }),
      tk('provision', { parent: 'root', status: 'passed' }),
      tk('T', { parent: 'root', status: 'pending', criteria: ['G-1'] }),
      tk('G-1', { parent: 'T', status: 'pending', role: 'criterion' }),
      tk('H-1', { parent: 'root', status: 'pending', role: 'criterion' }),
    ]
    const m = mount(ts)
    for (const id of ['provision', 'G-1', 'H-1']) {
      const row = m.row(id) as HTMLElement
      expect(row).not.toBeNull()
      expect(toggleButtons(row)).toHaveLength(0)
      expect(row.querySelector('[aria-expanded]')).toBeNull()
      expect(row.hasAttribute('data-expanded')).toBe(false)
      expect(row.querySelector('[data-part="hidden-count"]')).toBeNull()
    }
    // T is pending (collapsed) with a criterion: it has a toggle and a hidden count
    expect(toggleButtons(m.row('T') as HTMLElement)).toHaveLength(1)
    expect(m.row('T')?.querySelector('[data-part="hidden-count"]')?.textContent).toContain('+1')
  })

  test('test_AC_2_row_gaining_a_subtask_gains_a_toggle', () => {
    const m = mount(sample('failed'))
    expect(toggleButtons(m.row('B') as HTMLElement)).toHaveLength(0)
    const ts = sample('failed')
    ts[3] = tk('B', { parent: 'root', status: 'failed', subtasks: [['B1']] })
    ts.push(tk('B1', { parent: 'B', status: 'failed' }))
    m.update(ts)
    const b = m.row('B') as HTMLElement
    expect(toggleButtons(b)).toHaveLength(1)
    expect(b.querySelector('[aria-expanded]')).not.toBeNull()
    expect(b.hasAttribute('data-expanded')).toBe(true)
  })
})

describe('AC-3 row-wide hover highlight', () => {
  test('test_AC_3_highlight_lives_on_the_li_only', () => {
    const m = mount(sample('failed'), { selected: 'B' })
    for (const id of m.ids() as string[]) {
      const li = m.row(id) as HTMLElement
      expect(classes(li)).toContain('hover:bg-muted')
      for (const el of li.querySelectorAll('*'))
        expect(el.getAttribute('class') ?? '').not.toContain('hover:bg-')
    }
  })

  test('test_AC_3_selected_row_li_has_bg_muted_and_open_button_aria_current', () => {
    const m = mount(sample('failed'), { selected: 'B' })
    expect(classes(m.row('B'))).toContain('bg-muted')
    expect(m.openButton('B').getAttribute('aria-current')).toBe('true')
    expect(classes(m.openButton('B'))).not.toContain('bg-muted')
    for (const id of ['root', 'A', 'A1']) expect(classes(m.row(id))).not.toContain('bg-muted')
  })
})

describe('AC-4 toggle and status slots align', () => {
  test('test_AC_4_every_row_has_one_toggle_slot_and_one_status_slot', () => {
    const m = mount(sample('failed'))
    for (const id of m.ids() as string[]) {
      const li = m.row(id) as HTMLElement
      const toggles = li.querySelectorAll('[data-part="toggle-slot"]')
      const statuses = li.querySelectorAll('[data-part="status-slot"]')
      expect(toggles).toHaveLength(1)
      expect(statuses).toHaveLength(1)
      expect(classes(toggles[0])).toContain('size-5')
      expect(classes(statuses[0])).toContain('size-5')
      expect(li.firstElementChild).toBe(toggles[0] as Element)
      expect(m.openButton(id).contains(statuses[0] as Element)).toBe(true)
      expect(toggles[0]?.contains(statuses[0] as Element)).toBe(false)
      expect(statuses[0]?.children.length).toBe(1)
    }
  })

  test('test_AC_4_indent_is_half_rem_plus_step_per_depth_same_for_leaf_and_parent', () => {
    const m = mount(sample('failed'))
    const depth: Record<string, number> = { root: 0, A: 1, A1: 2, B: 1 }
    for (const id of m.ids() as string[]) {
      const pl = (m.row(id) as HTMLElement).style.paddingLeft
      expect(pl.endsWith('rem')).toBe(true)
      expect(parseFloat(pl)).toBeCloseTo(0.5 + (depth[id] as number) * 1.25, 5)
    }
    // leaf B and parent A share depth 1
    expect((m.row('B') as HTMLElement).style.paddingLeft).toBe(
      (m.row('A') as HTMLElement).style.paddingLeft,
    )
  })
})

describe('AC-5 behaviour preserved', () => {
  test('test_AC_5_open_button_opens_once_toggle_does_not', async () => {
    const m = mount(sample('failed'))
    await m.click('A')
    await m.click('A')
    expect(m.opened).toEqual([])
    await m.user.click(m.openButton('A'))
    expect(m.opened).toEqual(['A'])
    await m.user.click(m.openButton('B'))
    expect(m.opened).toEqual(['A', 'B'])
  })

  test('test_AC_5_toggling_hides_and_shows_descendants', async () => {
    const m = mount(sample('failed'))
    await m.click('A')
    expect(m.ids()).toEqual(['root', 'A', 'B'])
    expect(m.toggle('A', false)?.getAttribute('aria-expanded')).toBe('false')
    await m.click('A')
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
    await m.click('root')
    expect(m.ids()).toEqual(['root'])
  })

  test('test_AC_5_aria_current_only_on_selected_open_button', () => {
    const m = mount(sample('failed'), { selected: 'A1' })
    expect(m.openButton('A1').getAttribute('aria-current')).toBe('true')
    for (const id of ['root', 'A', 'B'])
      expect(m.openButton(id).hasAttribute('aria-current')).toBe(false)
    expect(m.row('A1')?.querySelectorAll('[aria-current]')).toHaveLength(1)
  })

  test('test_AC_5_collapsed_parent_shows_count_badges_and_selection', async () => {
    const m = mount(sample('waiting'), { signoffs: ['A1'], selected: 'A1' })
    expect(m.row('A')?.hasAttribute('data-contains-selected')).toBe(false)
    await m.click('A')
    expect(m.ids()).toEqual(['root', 'A', 'B'])
    const a = m.row('A') as HTMLElement
    expect(a.querySelector('[data-part="hidden-count"]')?.textContent).toContain('+1')
    expect(a.textContent).toContain('sign-off due')
    expect(a.textContent).toMatch(/waiting|proposal/)
    expect(a.getAttribute('data-contains-selected')).toBe('true')
    expect(m.row('B')?.querySelector('[data-part="hidden-count"]')).toBeNull()
  })

  test('test_AC_5_active_leaf_opens_ancestors', async () => {
    for (const active of ['working', 'checking', 'merging'] as const) {
      const m = mount(sample('failed'))
      await m.click('A')
      await m.click('root')
      expect(m.ids()).toEqual(['root'])
      m.update(sample(active))
      expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
      expect(expanded(m, 'root')).toBe('true')
      expect(expanded(m, 'A')).toBe('true')
      cleanup()
    }
    const m = mount(sample('working'))
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
  })

  test('test_AC_5_passed_and_pending_collapse_and_manual_choice_persists', async () => {
    const m = mount(sample('failed'))
    expect(expanded(m, 'A')).toBe('true')
    const ts = sample('failed')
    ts[1] = tk('A', { parent: 'root', status: 'passed', subtasks: [['A1']] })
    m.update(ts)
    expect(expanded(m, 'A')).toBe('false')
    expect(m.ids()).toEqual(['root', 'A', 'B'])
    await m.click('A') // manual open
    expect(expanded(m, 'A')).toBe('true')
    m.update(ts)
    m.update(ts)
    expect(expanded(m, 'A')).toBe('true')
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
    cleanup()
    const p = mount(sample('pending'))
    expect(expanded(p, 'A')).toBe('true')
    const ts2 = sample('failed')
    ts2[0] = tk('root', { status: 'pending', subtasks: [['A'], ['B']] })
    p.update(ts2)
    expect(expanded(p, 'root')).toBe('false')
  })
})
