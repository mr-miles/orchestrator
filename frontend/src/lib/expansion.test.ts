import { describe, expect, test } from 'bun:test'
import { ticket } from '@/test/fakes'
import type { Ticket } from './types'
import {
  ACTIVE_STATUSES,
  descendantIds,
  isLeaf,
  isOpen,
  reconcile,
  toggle,
  visibleRows,
} from './expansion'
import { treeRows } from './tickets'

const t = (id: string, o: Partial<Ticket> = {}) => ticket({ id, type: 'work', ...o })

/** root > A > A1, root > B, root > criterion C */
const tree = (a1: Ticket['status'], b: Ticket['status'] = 'failed'): Ticket[] => [
  t('root', { subtasks: [['A', 'B']], criteria: ['C'], status: 'subtasks' }),
  t('A', { parent: 'root', subtasks: [['A1']], status: 'subtasks' }),
  t('A1', { parent: 'A', status: a1 }),
  t('B', { parent: 'root', status: b }),
  t('C', { parent: 'root', status: 'failed' }),
]

describe('reconcile', () => {
  test('opens an active leaf and its ancestors', () => {
    for (const s of ACTIVE_STATUSES) {
      const prev = toggle(toggle(reconcile(tree('failed'), null), 'A'), 'root')
      const e = reconcile(tree(s), prev)
      expect(['root', 'A', 'A1'].map((id) => isOpen(e, id))).toEqual([true, true, true])
    }
  })

  test('waiting, paused and non-leaf tickets do not open', () => {
    const prev = toggle(reconcile(tree('failed'), null), 'A1')
    expect(isOpen(reconcile(tree('waiting'), prev), 'A1')).toBe(false)
    const ts = tree('failed')
    ts[1] = t('A', { parent: 'root', subtasks: [['A1']], status: 'working' })
    expect(isOpen(reconcile(ts, toggle(reconcile(tree('failed'), null), 'A')), 'A')).toBe(false)
  })

  test('passed and pending collapse, including on first sight', () => {
    expect(isOpen(reconcile(tree('passed'), null), 'A1')).toBe(false)
    expect(isOpen(reconcile(tree('pending'), null), 'A1')).toBe(false)
    expect(isOpen(reconcile(tree('passed'), reconcile(tree('working'), null)), 'A1')).toBe(false)
  })

  test('failed tickets stay open', () => {
    expect(isOpen(reconcile(tree('failed'), null), 'A1')).toBe(true)
  })

  test('returns prev itself and keeps manual toggles while statuses are unchanged', () => {
    const e = toggle(reconcile(tree('working'), null), 'A1')
    const n = reconcile(tree('working'), e)
    expect(n).toBe(e)
    expect(isOpen(n, 'A1')).toBe(false)
  })

  test('a status change overrides a manual choice and does not mutate its inputs', () => {
    const e = toggle(reconcile(tree('failed', 'working'), null), 'B')
    const snapshot = JSON.stringify(e)
    const ts = tree('failed', 'passed')
    const tsSnapshot = JSON.stringify(ts)
    const n = reconcile(ts, e)
    expect(n).not.toBe(e)
    expect(JSON.stringify(e)).toBe(snapshot)
    expect(JSON.stringify(ts)).toBe(tsSnapshot)
    expect(isOpen(n, 'B')).toBe(false)
    expect(n.status.B).toBe('passed')
  })

  test('a ticket unchanged keeps its choice when another changes', () => {
    const e = toggle(reconcile(tree('failed', 'working'), null), 'A')
    expect(isOpen(reconcile(tree('failed', 'merging'), e), 'A')).toBe(false)
  })

  test('survives a parent cycle', () => {
    const ts = [
      t('x', { parent: 'y', status: 'working' }),
      t('y', { parent: 'x', status: 'failed' }),
    ]
    expect(isOpen(reconcile(ts, null), 'y')).toBe(true)
  })
})

describe('toggle', () => {
  test('flips the state and keeps statuses', () => {
    const e = reconcile(tree('failed'), null)
    const c = toggle(e, 'A')
    expect(isOpen(c, 'A')).toBe(false)
    expect(isOpen(e, 'A')).toBe(true)
    expect(c.status).toEqual(e.status)
    expect(isOpen(toggle(c, 'A'), 'A')).toBe(true)
  })
})

describe('visibleRows', () => {
  const rows = treeRows(tree('failed'))
  const ids = (e: ReturnType<typeof reconcile>) => visibleRows(rows, e).map((r) => r.ticket.id)

  test('shows everything when open', () => {
    expect(ids(reconcile(tree('failed'), null))).toEqual(['root', 'A', 'A1', 'B', 'C'])
  })

  test('hides descendants of collapsed rows', () => {
    const e = toggle(reconcile(tree('failed'), null), 'A')
    expect(ids(e)).toEqual(['root', 'A', 'B', 'C'])
    expect(ids(toggle(e, 'root'))).toEqual(['root'])
  })
})

describe('descendantIds and isLeaf', () => {
  test('walks subtask groups and criteria recursively', () => {
    const ts = tree('failed')
    expect(descendantIds(ts, 'root').sort()).toEqual(['A', 'A1', 'B', 'C'])
    expect(descendantIds(ts, 'A1')).toEqual([])
    expect(descendantIds(ts, 'nope')).toEqual([])
  })

  test('guards against cycles', () => {
    const ts = [t('x', { subtasks: [['y']] }), t('y', { subtasks: [['x']] })]
    expect(descendantIds(ts, 'x')).toEqual(['y'])
  })

  test('a leaf has neither subtasks nor criteria', () => {
    const ts = tree('failed')
    expect(isLeaf(ts[2]!)).toBe(true)
    expect(isLeaf(ts[1]!)).toBe(false)
    expect(isLeaf(t('X', { criteria: ['c'] }))).toBe(false)
  })
})
