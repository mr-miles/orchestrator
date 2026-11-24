import { afterEach, describe, expect, test } from 'bun:test'
import { cleanup, within } from '@testing-library/react'
import { expanded, hasGoal, mount, sample, tk } from './support/tree'

afterEach(cleanup)

describe('AC-1 toggle buttons', () => {
  test('test_AC_1_every_row_has_a_toggle_with_aria_expanded', () => {
    const m = mount(sample('failed'))
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
    for (const id of m.ids()) {
      const t = m.toggle(id as string, true)
      expect(t).not.toBeNull()
      expect(t?.tagName).toBe('BUTTON')
      expect(t?.getAttribute('aria-expanded')).toBe('true')
    }
  })

  test('test_AC_1_toggling_a_parent_hides_and_shows_its_descendants', async () => {
    const m = mount(sample('failed'))
    await m.click('A')
    expect(m.ids()).toEqual(['root', 'A', 'B'])
    expect(m.toggle('A', false)?.getAttribute('aria-expanded')).toBe('false')
    await m.click('A')
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
    await m.click('root')
    expect(m.ids()).toEqual(['root'])
  })

  test('test_AC_1_leaf_goal_shown_only_when_expanded', async () => {
    const m = mount(sample('failed'))
    expect(hasGoal(m, 'A1')).toBe(true)
    await m.click('A1')
    expect(m.row('A1')).not.toBeNull()
    expect(hasGoal(m, 'A1')).toBe(false)
    await m.click('A1')
    expect(hasGoal(m, 'A1')).toBe(true)
  })
})

describe('AC-2 active leaves expand', () => {
  test('test_AC_2_leaf_becoming_active_expands_itself_and_ancestors', async () => {
    for (const active of ['working', 'checking', 'merging'] as const) {
      const m = mount(sample('failed'))
      await m.click('A1')
      await m.click('A')
      await m.click('root')
      expect(m.ids()).toEqual(['root'])
      m.update(sample(active))
      expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
      expect(expanded(m, 'root')).toBe('true')
      expect(expanded(m, 'A')).toBe('true')
      expect(expanded(m, 'A1')).toBe('true')
      expect(hasGoal(m, 'A1')).toBe(true)
      cleanup()
    }
  })

  test('test_AC_2_leaf_already_active_on_first_render_is_expanded', () => {
    const m = mount(sample('working'))
    expect(expanded(m, 'A1')).toBe('true')
    expect(hasGoal(m, 'A1')).toBe(true)
    expect(m.ids()).toEqual(['root', 'A', 'A1', 'B'])
  })

  test('test_AC_2_waiting_paused_and_non_leaf_do_not_expand', async () => {
    for (const s of ['waiting', 'paused'] as const) {
      const m = mount(sample('failed'))
      await m.click('A1')
      await m.click('A')
      m.update(sample(s))
      expect(m.ids()).toEqual(['root', 'A', 'B'])
      cleanup()
    }
    // a non-leaf that becomes working is not forced open
    const m = mount(sample('failed'))
    await m.click('A')
    const ts = sample('failed')
    ts[1] = tk('A', { parent: 'root', status: 'working', subtasks: [['A1']] })
    m.update(ts)
    expect(expanded(m, 'A')).toBe('false')
    expect(m.ids()).toEqual(['root', 'A', 'B'])
  })
})

describe('AC-3 passed and pending collapse', () => {
  test('test_AC_3_ticket_becoming_passed_is_collapsed_but_still_shown', () => {
    const m = mount(sample('working'))
    expect(expanded(m, 'A1')).toBe('true')
    m.update(sample('passed'))
    expect(expanded(m, 'A1')).toBe('false')
    const row = within(m.row('A1') as HTMLElement)
    expect(row.getByText('A1')).not.toBeNull()
    expect(m.row('A1')?.textContent).toContain('passed')
    expect(hasGoal(m, 'A1')).toBe(false)
  })

  test('test_AC_3_pending_and_passed_parents_collapsed_on_first_render', () => {
    const m = mount(sample('pending'))
    expect(expanded(m, 'A1')).toBe('false')
    expect(hasGoal(m, 'A1')).toBe(false)
    cleanup()
    const ts = sample('failed')
    ts[1] = tk('A', { parent: 'root', status: 'passed', subtasks: [['A1']] })
    const m2 = mount(ts)
    expect(expanded(m2, 'A')).toBe('false')
    expect(m2.ids()).toEqual(['root', 'A', 'B'])
  })

  test('test_AC_3_failed_and_waiting_are_not_collapsed', () => {
    const m = mount(sample('failed', 'waiting'))
    expect(expanded(m, 'A1')).toBe('true')
    expect(expanded(m, 'B')).toBe('true')
    expect(hasGoal(m, 'A1')).toBe(true)
    expect(hasGoal(m, 'B')).toBe(true)
  })
})

describe('AC-4 manual choice', () => {
  test('test_AC_4_any_node_toggles_either_way_whatever_its_status', async () => {
    const m = mount(sample('pending', 'passed'))
    // collapsed by the rules -> expand by hand
    expect(expanded(m, 'A1')).toBe('false')
    await m.click('A1')
    expect(expanded(m, 'A1')).toBe('true')
    expect(hasGoal(m, 'A1')).toBe(true)
    await m.click('B')
    expect(expanded(m, 'B')).toBe('true')
    // open by default -> collapse by hand
    await m.click('root')
    expect(expanded(m, 'root')).toBe('false')
    cleanup()
    const w = mount(sample('working'))
    await w.click('A1')
    expect(expanded(w, 'A1')).toBe('false')
  })

  test('test_AC_4_manual_choice_survives_rerender_with_same_statuses', async () => {
    const m = mount(sample('working', 'passed'))
    await m.click('A1')
    await m.click('B')
    m.update(sample('working', 'passed'))
    m.update(sample('working', 'passed'))
    expect(expanded(m, 'A1')).toBe('false')
    expect(expanded(m, 'B')).toBe('true')
  })

  test('test_AC_4_status_change_overrides_manual_choice', async () => {
    const m = mount(sample('failed', 'working'))
    await m.click('B') // collapse the working leaf by hand
    expect(expanded(m, 'B')).toBe('false')
    m.update(sample('failed', 'passed'))
    expect(expanded(m, 'B')).toBe('false')
    await m.click('B')
    expect(expanded(m, 'B')).toBe('true')
    m.update(sample('failed', 'checking'))
    expect(expanded(m, 'B')).toBe('true')
    await m.click('B')
    m.update(sample('failed', 'merging'))
    expect(expanded(m, 'B')).toBe('true')
    // a ticket whose status did not change keeps its choice
    expect(expanded(m, 'A1')).toBe('true')
  })
})

describe('AC-5 existing behaviour preserved', () => {
  test('test_AC_5_row_opens_ticket_once_toggle_does_not', async () => {
    const m = mount(sample('failed'))
    await m.click('A1')
    await m.click('A1')
    expect(m.opened).toEqual([])
    await m.user.click(m.openButton('A1'))
    expect(m.opened).toEqual(['A1'])
  })

  test('test_AC_5_selected_row_has_aria_current', () => {
    const m = mount(sample('failed'), { selected: 'B' })
    expect(m.openButton('B').getAttribute('aria-current')).toBe('true')
    for (const id of ['root', 'A', 'A1'])
      expect(m.openButton(id).hasAttribute('aria-current')).toBe(false)
  })

  test('test_AC_5_collapsed_row_keeps_id_kind_attempts_and_state', async () => {
    const ts = sample('failed')
    ts[2] = tk('A1', { parent: 'A', status: 'failed', type: 'agent:planner', attempt: 2 })
    const m = mount(ts)
    await m.click('A1')
    const text = m.row('A1')?.textContent ?? ''
    expect(expanded(m, 'A1')).toBe('false')
    expect(text).toContain('A1')
    expect(text).toContain('planner')
    expect(text).toContain('2/3')
    expect(text).toContain('failed')
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
    // a leaf has no hidden count
    expect(m.row('B')?.querySelector('[data-part="hidden-count"]')).toBeNull()
  })
})
