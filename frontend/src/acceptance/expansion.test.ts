import { describe, expect, test } from 'bun:test'
import {
  descendantIds,
  isLeaf,
  isOpen,
  reconcile,
  toggle,
  visibleRows,
  type Expansion,
} from '@/lib/expansion'
import { treeRows } from '@/lib/tickets'
import { sample, tk } from './support/tree'

const R = (a1: Parameters<typeof sample>[0], b: Parameters<typeof sample>[1] = 'failed') =>
  reconcile(sample(a1, b), null)

describe('AC-1 toggling and visibility', () => {
  test('test_AC_1_toggle_flips_and_visible_rows_drop_descendants', () => {
    const ts = sample('failed')
    const e0 = R('failed')
    expect(isOpen(e0, 'A')).toBe(true)
    const rows = treeRows(ts)
    expect(visibleRows(rows, e0).map((r) => r.ticket.id)).toEqual(['root', 'A', 'A1', 'B'])
    const e1 = toggle(e0, 'A')
    expect(isOpen(e1, 'A')).toBe(false)
    expect(isOpen(e0, 'A')).toBe(true)
    expect(visibleRows(rows, e1).map((r) => r.ticket.id)).toEqual(['root', 'A', 'B'])
    expect(visibleRows(rows, toggle(e1, 'root')).map((r) => r.ticket.id)).toEqual(['root'])
    expect(isOpen(toggle(e1, 'A'), 'A')).toBe(true)
  })

  test('test_AC_1_descendants_and_leaf', () => {
    const ts = sample('failed')
    expect(descendantIds(ts, 'root').sort()).toEqual(['A', 'A1', 'B'])
    expect(descendantIds(ts, 'A1')).toEqual([])
    expect(isLeaf(ts[2]!)).toBe(true)
    expect(isLeaf(ts[1]!)).toBe(false)
    expect(isLeaf(tk('X', { criteria: ['c'] }))).toBe(false)
  })
})

describe('AC-2 active leaves', () => {
  test('test_AC_2_active_leaf_opens_itself_and_ancestors', () => {
    for (const s of ['working', 'checking', 'merging'] as const) {
      let e: Expansion = R('failed')
      e = toggle(toggle(toggle(e, 'A1'), 'A'), 'root')
      const n = reconcile(sample(s), e)
      for (const id of ['root', 'A', 'A1']) expect(isOpen(n, id)).toBe(true)
    }
    for (const id of ['root', 'A', 'A1']) expect(isOpen(R('working'), id)).toBe(true)
  })

  test('test_AC_2_waiting_paused_and_non_leaf_do_not_open', () => {
    for (const s of ['waiting', 'paused'] as const) {
      const e = toggle(toggle(R('failed'), 'A1'), 'A')
      const n = reconcile(sample(s), e)
      expect(isOpen(n, 'A1')).toBe(false)
      expect(isOpen(n, 'A')).toBe(false)
    }
    const e = toggle(R('failed'), 'A')
    const ts = sample('failed')
    ts[1] = tk('A', { parent: 'root', status: 'working', subtasks: [['A1']] })
    expect(isOpen(reconcile(ts, e), 'A')).toBe(false)
  })
})

describe('AC-3 passed and pending', () => {
  test('test_AC_3_passed_collapses_on_transition_and_first_render', () => {
    const e = reconcile(sample('passed'), R('working'))
    expect(isOpen(e, 'A1')).toBe(false)
    expect(isOpen(R('pending'), 'A1')).toBe(false)
    const ts = sample('failed')
    ts[1] = tk('A', { parent: 'root', status: 'passed', subtasks: [['A1']] })
    expect(isOpen(reconcile(ts, null), 'A')).toBe(false)
  })

  test('test_AC_3_failed_and_waiting_not_collapsed', () => {
    const e = R('failed', 'waiting')
    expect(isOpen(e, 'A1')).toBe(true)
    expect(isOpen(e, 'B')).toBe(true)
    expect(isOpen(reconcile(sample('failed', 'failed'), R('working', 'working')), 'B')).toBe(true)
  })
})

describe('AC-4 manual choice vs rules', () => {
  test('test_AC_4_same_statuses_keep_choice_and_same_reference', () => {
    const ts = sample('working')
    const e = toggle(R('working'), 'A1')
    expect(isOpen(e, 'A1')).toBe(false)
    const n = reconcile(ts, e)
    expect(n).toBe(e)
    expect(isOpen(n, 'A1')).toBe(false)
  })

  test('test_AC_4_status_change_overrides_and_inputs_are_not_mutated', () => {
    const e = toggle(R('failed', 'working'), 'B')
    const snapshot = JSON.stringify(e)
    const n = reconcile(sample('failed', 'passed'), e)
    expect(n).not.toBe(e)
    expect(JSON.stringify(e)).toBe(snapshot)
    expect(isOpen(n, 'B')).toBe(false)
    const m = reconcile(sample('failed', 'checking'), toggle(n, 'B'))
    expect(isOpen(m, 'B')).toBe(true)
    // an untouched ticket keeps its manual choice across another ticket's change
    const kept = reconcile(sample('failed', 'merging'), toggle(m, 'A'))
    expect(isOpen(kept, 'A')).toBe(false)
  })
})

describe('AC-6 module is importable and typed', () => {
  test('test_AC_6_exports_the_contract', async () => {
    const mod = await import('@/lib/expansion')
    expect([...mod.ACTIVE_STATUSES].sort()).toEqual(['checking', 'merging', 'working'])
  })
})
