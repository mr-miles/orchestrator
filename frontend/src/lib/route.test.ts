import { describe, expect, test } from 'bun:test'
import { formatHash, parseHash, samePage } from './route'

describe('routes', () => {
  test('every page round-trips through its hash', () => {
    for (const h of [
      '#/work',
      '#/inbox',
      '#/config/skills',
      '#/config/agents',
      '#/ws/app',
      '#/ws/app/documents',
      '#/ws/app/repository',
      '#/ws/app/c/c1/agents',
    ])
      expect(formatHash(parseHash(h))).toBe(h)
  })

  test('a change link opens its ticket once; the hash keeps the page only', () => {
    const r = parseHash('#/ws/app/c/c1/overview/H-2')
    expect(r).toMatchObject({ page: 'change', ws: 'app', cid: 'c1', tab: 'overview' })
    expect(r.page === 'change' && r.focus?.ticket).toBe('H-2')
    expect(formatHash(r)).toBe('#/ws/app/c/c1/overview')
  })

  test('an old-style link (notifications) is a change in an unknown workspace', () => {
    const r = parseHash('#c1/overview/H-1')
    expect(r).toMatchObject({ page: 'change', ws: '', cid: 'c1', tab: 'overview' })
    expect(r.page === 'change' && r.focus?.ticket).toBe('H-1')
  })

  test('an old-style link is the same page as its change in a workspace', () => {
    expect(samePage(parseHash('#c1/overview'), parseHash('#/ws/app/c/c1/overview'))).toBe(true)
    expect(samePage(parseHash('#c1/agents'), parseHash('#/ws/app/c/c1/overview'))).toBe(false)
    expect(samePage(parseHash('#/ws/a/c/c1/overview'), parseHash('#/ws/b/c/c1/overview'))).toBe(
      false,
    )
  })

  test('anything else is the work page', () => {
    for (const h of ['', '#', '#/', '#/nope', '#/config/secrets', '#/ws'])
      expect(parseHash(h)).toEqual({ page: 'work' })
    expect(parseHash('#/ws/app/bogus')).toEqual({ page: 'workspace', ws: 'app', tab: 'changes' })
    expect(parseHash('#/ws/app/c/c1/bogus')).toMatchObject({ page: 'change', tab: 'overview' })
  })
})
