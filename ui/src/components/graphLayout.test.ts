import { describe, expect, it } from 'vitest'
import { boxFor, iterationsFor, layout, radiusFor } from './graphLayout'

const BOX = { width: 900, height: 460 }

function ring(count: number) {
  return Array.from({ length: count }, (_, i) => ({ id: `n${i}` }))
}

/** Pairs of nodes drawn closer together than their two circles can be without touching. */
function overlaps(
  nodes: Array<{ id: string }>,
  edges: Array<{ source: string; target: string }>,
  box: { width: number; height: number },
): number {
  const placed = layout(nodes, edges, box)
  const degree = new Map<string, number>()
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1)
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1)
  }
  let found = 0
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = placed.get(nodes[i]!.id)!
      const b = placed.get(nodes[j]!.id)!
      const room =
        radiusFor(degree.get(nodes[i]!.id) ?? 0) + radiusFor(degree.get(nodes[j]!.id) ?? 0)
      if (Math.hypot(a.x - b.x, a.y - b.y) < room) found += 1
    }
  }
  return found
}

describe('layout', () => {
  it('is identical across runs, which is what makes a screenshot of it worth pasting', () => {
    const nodes = ring(24)
    const edges = nodes.slice(1).map((node) => ({ source: 'n0', target: node.id }))
    const first = layout(nodes, edges, BOX)
    const second = layout(nodes, edges, BOX)
    for (const node of nodes) {
      expect(first.get(node.id)).toEqual(second.get(node.id))
    }
  })

  it('places every node, including ones no edge touches', () => {
    const nodes = ring(10)
    // Only the first three are connected; the rest are isolated and are still real answers.
    const edges = [
      { source: 'n0', target: 'n1' },
      { source: 'n1', target: 'n2' },
    ]
    const placed = layout(nodes, edges, BOX)
    expect(placed.size).toBe(10)
    for (const node of nodes) {
      const point = placed.get(node.id)
      expect(point).toBeDefined()
      expect(Number.isFinite(point!.x)).toBe(true)
      expect(Number.isFinite(point!.y)).toBe(true)
    }
  })

  it('keeps everything inside the box it was given', () => {
    const nodes = ring(40)
    const edges = nodes.slice(1).map((node, i) => ({ source: nodes[i]!.id, target: node.id }))
    const placed = layout(nodes, edges, BOX)
    for (const point of placed.values()) {
      expect(point.x).toBeGreaterThanOrEqual(0)
      expect(point.x).toBeLessThanOrEqual(BOX.width)
      expect(point.y).toBeGreaterThanOrEqual(0)
      expect(point.y).toBeLessThanOrEqual(BOX.height)
    }
  })

  it('draws connected nodes closer than unconnected ones', () => {
    // The one behavioural claim the picture rests on: an edge has to mean proximity, or the graph
    // is a decoration and every reading of it is wrong.
    const nodes = [{ id: 'a' }, { id: 'b' }, { id: 'x' }, { id: 'y' }]
    const placed = layout(nodes, [{ source: 'a', target: 'b' }], BOX)
    const distance = (p: string, q: string) =>
      Math.hypot(placed.get(p)!.x - placed.get(q)!.x, placed.get(p)!.y - placed.get(q)!.y)
    expect(distance('a', 'b')).toBeLessThan(distance('x', 'y'))
  })

  it('centres a lone node and returns nothing for none', () => {
    expect(layout([{ id: 'only' }], [], BOX).get('only')).toEqual({ x: 450, y: 230 })
    expect(layout([], [], BOX).size).toBe(0)
  })

  it('ignores an edge naming a node that is not in the result', () => {
    // A subgraph is filtered server-side, so an edge can outlive one of its endpoints in flight.
    const placed = layout(ring(3), [{ source: 'n0', target: 'gone' }], BOX)
    expect(placed.size).toBe(3)
  })

  it('spends less per node as the graph grows, because the step is quadratic', () => {
    expect(iterationsFor(2)).toBe(0)
    expect(iterationsFor(20)).toBeGreaterThan(iterationsFor(400))
    expect(iterationsFor(400)).toBeGreaterThanOrEqual(40)
  })

  it('does not draw one dot on top of another', () => {
    // The complaint this was rebuilt for: clicking a circle did nothing, because the circle was six
    // circles and the click landed on whichever was painted last. A hub with thirty leaves is the
    // shape that produced it — a file and every rule under it.
    const nodes = ring(120)
    const edges = nodes.slice(1).map((node, i) => ({ source: `n${i % 4}`, target: node.id }))
    expect(overlaps(nodes, edges, boxFor(nodes.length))).toBe(0)
  })

  it('does not crush a big component because a stray pair sits in a corner', () => {
    // One simulation over the whole result put unconnected pairs on the boundary and then scaled the
    // picture by a bounding box they defined, so 300 connected nodes ended up in a disc in the
    // middle. Components are laid out and packed separately now, and the test of that is that the
    // drawing uses the frame it was given.
    const core = ring(60)
    const edges = core.slice(1).map((node, i) => ({ source: core[i]!.id, target: node.id }))
    const strays = Array.from({ length: 8 }, (_, i) => ({ id: `s${i}` }))
    const nodes = [...core, ...strays]
    const box = boxFor(nodes.length)
    const placed = layout(nodes, edges, box)
    const coreX = core.map((n) => placed.get(n.id)!.x)
    const coreY = core.map((n) => placed.get(n.id)!.y)
    const spanX = Math.max(...coreX) - Math.min(...coreX)
    const spanY = Math.max(...coreY) - Math.min(...coreY)
    expect(spanX * spanY).toBeGreaterThan(box.width * box.height * 0.2)
  })

  it('never draws two unconnected nodes closer than two connected ones', () => {
    // The one claim the picture rests on. Packing components side by side is what threatened it:
    // two orphans in adjacent cells must not end up nearer than a pair with an edge.
    const nodes = [
      { id: 'a' },
      { id: 'b' },
      ...Array.from({ length: 12 }, (_, i) => ({ id: `o${i}` })),
    ]
    const placed = layout(nodes, [{ source: 'a', target: 'b' }], BOX)
    const gap = (p: string, q: string) =>
      Math.hypot(placed.get(p)!.x - placed.get(q)!.x, placed.get(p)!.y - placed.get(q)!.y)
    const linked = gap('a', 'b')
    let closest = Infinity
    for (let i = 0; i < 12; i += 1) {
      for (let j = i + 1; j < 12; j += 1) closest = Math.min(closest, gap(`o${i}`, `o${j}`))
    }
    expect(linked).toBeLessThan(closest)
  })

  it('stays quick enough to run on every keystroke of a query', () => {
    // The server caps a query at 400 nodes, so this is the worst case that can reach the browser.
    const nodes = ring(400)
    const edges = nodes.slice(1).map((node, i) => ({ source: nodes[i]!.id, target: node.id }))
    const started = performance.now()
    layout(nodes, edges, boxFor(nodes.length))
    expect(performance.now() - started).toBeLessThan(2000)
  })
})

describe('boxFor', () => {
  it('grows the box with the node count, which is what shrinks the dots', () => {
    // A node's radius is in layout units and the SVG scales its box to the panel, so the only lever
    // that gives three hundred dots room is a bigger box.
    expect(boxFor(10)).toEqual({ width: 900, height: 460 })
    expect(boxFor(400).width).toBeGreaterThan(boxFor(70).width)
  })

  it('keeps the shape of the panel, so a bigger graph is drawn further away, not taller', () => {
    const small = boxFor(20)
    const large = boxFor(400)
    expect(large.width / large.height).toBeCloseTo(small.width / small.height, 2)
  })
})

describe('radiusFor', () => {
  it('grows with degree and stops', () => {
    expect(radiusFor(0)).toBeLessThan(radiusFor(4))
    expect(radiusFor(4)).toBeLessThan(radiusFor(20))
    expect(radiusFor(1000)).toBeLessThanOrEqual(11)
  })
})
