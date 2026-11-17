import { describe, expect, test } from 'bun:test'
import { layout, type Layout, type LayoutNode } from './layout'

/** The pipeline around a task graph, as the engine builds it (graph.materialize_builds). */
function changeGraph(tasks: Record<string, string[]>): LayoutNode[] {
  const nodes: LayoutNode[] = [
    { id: 'intake', depends_on: [] },
    { id: 'H-1', depends_on: ['intake'] },
    { id: 'plan', depends_on: ['H-1'] },
    { id: 'oracle', depends_on: ['plan'] },
    { id: 'H-2', depends_on: ['plan', 'oracle'] },
  ]
  for (const [t, deps] of Object.entries(tasks))
    nodes.push({ id: `build:${t}`, depends_on: ['H-2', ...deps.map((d) => `build:${d}`)] })
  nodes.push(
    { id: 'integration', depends_on: Object.keys(tasks).map((t) => `build:${t}`) },
    { id: 'H-3', depends_on: ['integration'] },
    { id: 'ship', depends_on: ['H-3'] },
  )
  return nodes
}

/** Sample an SVG path made of M, L and C commands. */
function pathPoints(path: string): [number, number][] {
  const toks = path.match(/[MCL]|-?[\d.]+/g) ?? []
  const num = (k: number) => Number(toks[k])
  const pts: [number, number][] = []
  let i = 0
  let cur: [number, number] = [0, 0]
  while (i < toks.length) {
    const cmd = toks[i++]
    if (cmd === 'M') {
      cur = [num(i), num(i + 1)]
      i += 2
    } else if (cmd === 'L') {
      const end: [number, number] = [num(i), num(i + 1)]
      i += 2
      for (let k = 0; k <= 20; k++)
        pts.push([cur[0] + ((end[0] - cur[0]) * k) / 20, cur[1] + ((end[1] - cur[1]) * k) / 20])
      cur = end
    } else {
      const c1 = [num(i), num(i + 1)] as const
      const c2 = [num(i + 2), num(i + 3)] as const
      const end: [number, number] = [num(i + 4), num(i + 5)]
      i += 6
      for (let k = 0; k <= 40; k++) {
        const u = k / 40
        const b = (j: 0 | 1) =>
          (1 - u) ** 3 * cur[j] +
          3 * (1 - u) ** 2 * u * c1[j] +
          3 * (1 - u) * u * u * c2[j] +
          u ** 3 * end[j]
        pts.push([b(0), b(1)])
      }
      cur = end
    }
  }
  return pts
}

function checkDrawing(out: Layout) {
  const { pos, W, H } = out
  const ids = Object.keys(pos)
  const p = (id: string) => pos[id] ?? { x: NaN, y: NaN }
  const overlaps = ids.flatMap((a, i) =>
    ids
      .slice(i + 1)
      .filter((b) => Math.abs(p(a).x - p(b).x) < W && Math.abs(p(a).y - p(b).y) < H)
      .map((b) => [a, b]),
  )
  expect(overlaps).toEqual([])
  const through = new Set<string>()
  for (const e of out.edges)
    for (const [x, y] of pathPoints(e.path))
      for (const id of ids)
        if (
          id !== e.from &&
          id !== e.to &&
          p(id).x + 1 < x &&
          x < p(id).x + W - 1 &&
          p(id).y + 1 < y &&
          y < p(id).y + H - 1
        )
          through.add(`${e.from}->${e.to} through ${id}`)
  expect([...through]).toEqual([])
  for (const q of Object.values(pos)) {
    expect(q.x).toBeGreaterThanOrEqual(0)
    expect(q.x + W).toBeLessThanOrEqual(out.width)
    expect(q.y).toBeGreaterThanOrEqual(0)
    expect(q.y + H).toBeLessThanOrEqual(out.height)
  }
}

// T1..T7 of a real plan (a Connect 4 app): two branches off T2 rejoin at T5; T7 hangs off T1.
const CONNECT4 = {
  T1: [],
  T2: ['T1'],
  T3: ['T2'],
  T4: ['T2'],
  T5: ['T3', 'T4'],
  T6: ['T5'],
  T7: ['T1'],
}

describe('task graph layout', () => {
  test('a branching task graph has no overlaps and no edges through boxes', () => {
    const out = layout(changeGraph(CONNECT4))
    checkDrawing(out)
    const drawn = new Set(out.edges.map((e) => `${e.from}->${e.to}`))
    // implied edges are not drawn: H-2 reaches T2..T7 through T1; plan reaches H-2 via oracle
    expect(drawn.has('H-2->build:T1')).toBe(true)
    expect(drawn.has('H-2->build:T5')).toBe(false)
    expect(drawn.has('plan->H-2')).toBe(false)
    expect(drawn.has('oracle->H-2')).toBe(true)
    expect(drawn.has('build:T7->integration')).toBe(true)
    expect(drawn.has('build:T1->integration')).toBe(false)
    // parallel branches share a column, side by side
    const pos = (id: string) => out.pos[id] ?? { x: NaN, y: NaN }
    expect(pos('build:T3').x).toBe(pos('build:T4').x)
    expect(pos('build:T3').x).not.toBe(pos('build:T5').x)
    expect(pos('build:T3').y).not.toBe(pos('build:T4').y)
  })

  const shapes: Record<string, Record<string, string[]>> = {
    independent: { T1: [], T2: [], T3: [], T4: [] },
    chain: Object.fromEntries([1, 2, 3, 4, 5, 6].map((i) => [`T${i}`, i > 1 ? [`T${i - 1}`] : []])),
    'fan out and in': { A: [], B: ['A'], C: ['A'], D: ['A'], E: ['B', 'C', 'D'], F: [] },
    mixed: { A: [], B: ['A'], C: ['B'], D: ['C'], E: ['A'], F: ['E', 'D'], G: ['A'] },
  }
  for (const [name, tasks] of Object.entries(shapes))
    test(`${name} draws cleanly`, () => checkDrawing(layout(changeGraph(tasks))))

  test('a single node and unknown dependencies', () => {
    const out = layout([{ id: 'intake', depends_on: ['gone'] }])
    expect(out.edges).toEqual([])
    expect(out.pos.intake?.x).toBe(12)
  })

  test('the retrospective comes after the last stage', () => {
    const out = layout([
      ...changeGraph({ T1: [] }),
      { id: 'retrospective', kind: 'retrospective', depends_on: [] },
    ])
    const ship = out.pos.ship?.x ?? 0
    expect(out.pos.retrospective?.x).toBeGreaterThan(ship)
  })
})
