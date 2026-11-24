import { describe, expect, test } from 'bun:test'
import { modelOf } from './status'

describe('modelOf', () => {
  test('what served the run, with its effort', () => {
    expect(modelOf({ model: 'opus', effort: 'high', model_served: 'claude-opus-5-5' })).toEqual({
      text: 'claude-opus-5-5 (high)',
      mismatch: false,
    })
  })
  test('another model than it asked for is marked', () => {
    expect(modelOf({ model: 'sonnet', model_served: 'claude-opus-5-5' })).toEqual({
      text: 'claude-opus-5-5, asked for sonnet',
      mismatch: true,
    })
  })
  test('what it asked for while nothing has served it; nothing when neither is known', () => {
    expect(modelOf({ model: 'opus' })?.text).toBe('opus')
    expect(modelOf({})).toBeNull()
  })
})
