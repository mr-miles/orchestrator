import { expect, test } from 'bun:test'
import { ago } from './attention'

test('ago reads as a wait', () => {
  const now = Date.parse('2026-11-18T10:00:00.000Z')
  expect(ago('2026-11-18T09:59:15.000Z', now)).toBe('45s')
  expect(ago('2026-11-18T09:48:00.000Z', now)).toBe('12m')
  expect(ago('2026-11-18T00:58:00.000Z', now)).toBe('9h 02m')
  expect(ago('2026-11-15T06:00:00.000Z', now)).toBe('3d 4h')
  expect(ago('', now)).toBe('')
  expect(ago('not a date', now)).toBe('')
})
