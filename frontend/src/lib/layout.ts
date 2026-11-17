// Layered DAG layout (Sugiyama-style) of the change's task graph. Columns are dependency depth.
// Edges implied through another dependency are not drawn (transitive reduction); an edge spanning
// several columns gets a thin lane of its own in each column it crosses, so no edge is drawn
// through a box; rows are ordered by barycenter sweeps to reduce crossings.

export interface LayoutNode {
  id: string
  kind?: string
  depends_on: string[]
}

export interface Point {
  x: number
  y: number
}

export interface Edge {
  from: string
  to: string
  path: string
}

export interface Layout {
  pos: Record<string, Point>
  W: number
  H: number
  edges: Edge[]
  width: number
  height: number
}

interface Item {
  id: string
  layer: number
  virtual: boolean
  up: string[]
  down: string[]
}

export const BOX = { W: 178, H: 46, GX: 54, GY: 14, VH: 8, GV: 6 } as const

export function layout(nodes: LayoutNode[]): Layout {
  const { W, H, GX, GY, VH, GV } = BOX
  if (!nodes.length) return { pos: {}, W, H, edges: [], width: 10, height: 10 }
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const deps = (id: string) =>
    (byId.get(id)?.depends_on ?? []).filter((x) => byId.has(x) && x !== id)
  const depth = new Map<string, number>()
  const d = (id: string): number => {
    const known = depth.get(id)
    if (known !== undefined) return known
    depth.set(id, 0) // guards against cycles
    const v = Math.max(0, ...deps(id).map((x) => d(x) + 1))
    depth.set(id, v)
    return v
  }
  const anc = new Map<string, Set<string>>()
  const ancestors = (id: string): Set<string> => {
    const known = anc.get(id)
    if (known) return known
    const s = new Set<string>()
    anc.set(id, s)
    for (const x of deps(id)) {
      s.add(x)
      ancestors(x).forEach((a) => s.add(a))
    }
    return s
  }
  nodes.forEach((n) => d(n.id))
  // the retrospective depends on nothing (it runs however the change ended) but comes last
  const last = Math.max(0, ...nodes.filter((n) => n.kind !== 'retrospective').map((n) => d(n.id)))
  nodes.filter((n) => n.kind === 'retrospective').forEach((n) => depth.set(n.id, last + 1))
  const depthOf = (id: string) => depth.get(id) ?? 0

  // transitive reduction: drop dep x of n when another dep of n already depends on x
  const kept: [string, string][] = []
  for (const n of nodes) {
    const ds = deps(n.id)
    for (const x of ds) if (!ds.some((o) => o !== x && ancestors(o).has(x))) kept.push([x, n.id])
  }

  // layers of real and virtual items; long edges become chains of virtual items
  const ncol = Math.max(...nodes.map((n) => depthOf(n.id))) + 1
  const layers: string[][] = Array.from({ length: ncol }, () => [])
  const item = new Map<string, Item>()
  const add = (id: string, layer: number, virtual: boolean) => {
    item.set(id, { id, layer, virtual, up: [], down: [] })
    layers[layer]?.push(id)
  }
  const get = (id: string) => item.get(id) as Item
  nodes.forEach((n) => add(n.id, depthOf(n.id), false))
  const chains = kept.map(([a, b], k) => {
    const chain = [a]
    for (let L = depthOf(a) + 1; L < depthOf(b); L++) {
      const v = `~${k}:${L}`
      add(v, L, true)
      chain.push(v)
    }
    chain.push(b)
    for (let i = 0; i + 1 < chain.length; i++) {
      const [p, q] = [chain[i] as string, chain[i + 1] as string]
      get(p).down.push(q)
      get(q).up.push(p)
    }
    return { from: a, to: b, chain }
  })

  // order rows: barycenter sweeps down and up (ties keep the current order)
  const rank = () => {
    const r = new Map<string, number>()
    layers.forEach((l) => l.forEach((id, i) => r.set(id, i)))
    return r
  }
  const sweep = (L: number, side: 'up' | 'down') => {
    const r = rank()
    const at = (id: string) => r.get(id) ?? 0
    const bc = (id: string) => {
      const ns = get(id)[side]
      return ns.length ? ns.reduce((s, x) => s + at(x), 0) / ns.length : at(id)
    }
    layers[L] = (layers[L] ?? [])
      .map((id, i) => [bc(id), i, id] as const)
      .sort((p, q) => p[0] - q[0] || p[1] - q[1])
      .map((x) => x[2])
  }
  for (let it = 0; it < 6; it++) {
    for (let L = 1; L < ncol; L++) sweep(L, 'up')
    for (let L = ncol - 2; L >= 0; L--) sweep(L, 'down')
  }

  // coordinates: boxes H tall, lanes VH tall; each column centred on the tallest
  const tall = (id: string) => (get(id).virtual ? VH : H)
  const gap = (id: string) => (get(id).virtual ? GV : GY)
  const colH = layers.map((l) => l.reduce((s, id) => s + tall(id) + gap(id), 0))
  const maxH = Math.max(...colH)
  const pos = new Map<string, Point>()
  layers.forEach((l, L) => {
    let y = 12 + (maxH - (colH[L] ?? 0)) / 2
    l.forEach((id) => {
      pos.set(id, { x: 12 + L * (W + GX), y })
      y += tall(id) + gap(id)
    })
  })
  const at = (id: string) => pos.get(id) as Point

  // edge routes: box right-middle -> through each lane (straight across a column) -> box left-middle
  const edges = chains.map(({ from, to, chain }) => {
    const pts: [number, number][] = [[at(from).x + W, at(from).y + H / 2]]
    chain.slice(1, -1).forEach((v) => {
      const cy = at(v).y + VH / 2
      pts.push([at(v).x, cy], [at(v).x + W, cy])
    })
    pts.push([at(to).x, at(to).y + H / 2])
    let path = `M${pts[0]?.[0]},${pts[0]?.[1]}`
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1] as [number, number]
      const [x1, y1] = pts[i] as [number, number]
      path +=
        i % 2 ? ` C${(x0 + x1) / 2},${y0} ${(x0 + x1) / 2},${y1} ${x1},${y1}` : ` L${x1},${y1}`
    }
    return { from, to, path }
  })
  const real = Object.fromEntries(nodes.map((n) => [n.id, at(n.id)]))
  return { pos: real, W, H, edges, width: 24 + ncol * (W + GX), height: 24 + maxH }
}
