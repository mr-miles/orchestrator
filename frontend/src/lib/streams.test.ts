import { expect, test } from 'bun:test'
import { applyLogUpdate, EMPTY_LOG } from './streams'
import type { LogEntry, LogUpdate } from './types'

const e = (text: string): LogEntry => ({ kind: 'text', text, error: false })
const upd = (from: number, entries: LogEntry[], live = true): LogUpdate => ({
  run: 'r1',
  status: live ? 'ACTIVE' : 'ACCEPTED',
  live,
  from,
  entries,
  total: from + entries.length,
})

test('a log update keeps the entries before `from` and replaces the rest', () => {
  let v = applyLogUpdate(EMPTY_LOG, upd(0, [e('a'), e('b')]))
  expect(v.entries.map((x) => x.text)).toEqual(['a', 'b'])
  v = applyLogUpdate(v, upd(1, [e('b2'), e('c')])) // the last entry grew, one more arrived
  expect(v.entries.map((x) => x.text)).toEqual(['a', 'b2', 'c'])
  v = applyLogUpdate(v, upd(0, [e('x')], false)) // the window moved: everything again
  expect(v.entries.map((x) => x.text)).toEqual(['x'])
  expect(v.update?.live).toBe(false)
})
