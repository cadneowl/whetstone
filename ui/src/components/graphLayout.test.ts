import { describe, expect, it } from 'vitest'
import { iterationsFor, layout, radiusFor } from './graphLayout'

const BOX = { width: 900, height: 460 }

function ring(count: number) {
  return Array.from({ length: count }, (_, i) => ({ id: `n${i}` }))
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
})

describe('radiusFor', () => {
  it('grows with degree and stops', () => {
    expect(radiusFor(0)).toBeLessThan(radiusFor(4))
    expect(radiusFor(4)).toBeLessThan(radiusFor(20))
    expect(radiusFor(1000)).toBeLessThanOrEqual(11)
  })
})
