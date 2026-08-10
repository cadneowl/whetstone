import { describe, expect, it } from 'vitest'
import { groupsOf } from '@/components/graph/Neighbours'
import type { GraphPalette } from '@/components/graph/types'

/**
 * What the card under the picture asserts a node is attached to.
 *
 * Worth its own test because being quietly wrong here is worse than drawing nothing: the list reads
 * as fact about the folder, and a duplicated or mis-directed row is a connection the graph does not
 * actually have.
 */

const PALETTE: GraphPalette = {
  colour: {},
  help: {},
  hollow: 'unresolved',
  anchors: [],
  // Order matters: the list is sorted by it, so the structural edges that recede in the picture
  // recede here too.
  edge: { contains: { opacity: 0.3 }, refers: { opacity: 0.9 }, cites: { opacity: 0.35 } },
  edgeHelp: {},
  edgeRelation: {
    contains: { out: 'holds', in: 'lives in' },
    refers: { out: 'names', in: 'named by' },
    cites: { out: 'came out of', in: 'produced' },
  },
}

const edges = [
  { source: 'file', target: 'R1', kind: 'contains' },
  { source: 'R1', target: 'R2', kind: 'refers' },
  { source: 'R3', target: 'R1', kind: 'refers' },
  { source: 'R1', target: 'mr-42', kind: 'cites' },
]

describe('groupsOf', () => {
  it('separates the two readings of one edge kind', () => {
    const groups = groupsOf('R1', edges, PALETTE)
    const named = groups.find((g) => g.phrase === 'names')
    const namedBy = groups.find((g) => g.phrase === 'named by')
    expect(named?.ids).toEqual(['R2'])
    expect(namedBy?.ids).toEqual(['R3'])
  })

  it('orders by the palette, so structure comes before what an author wrote', () => {
    expect(groupsOf('R1', edges, PALETTE).map((g) => g.phrase)).toEqual([
      'lives in',
      'names',
      'named by',
      'came out of',
    ])
  })

  it('lists a neighbour once however many edges of a kind reach it', () => {
    const doubled = [...edges, { source: 'R1', target: 'R2', kind: 'refers' }]
    expect(groupsOf('R1', doubled, PALETTE).find((g) => g.phrase === 'names')?.ids).toEqual(['R2'])
  })

  it('drops a self-edge rather than claiming a node is attached to something', () => {
    const loop = [{ source: 'R1', target: 'R1', kind: 'refers' }]
    expect(groupsOf('R1', loop, PALETTE)).toEqual([])
  })

  it('ignores edges that do not touch the node at all', () => {
    expect(groupsOf('elsewhere', edges, PALETTE)).toEqual([])
  })

  it('falls back to the bare kind for an edge the palette has not learnt', () => {
    const unknown = [{ source: 'R1', target: 'X', kind: 'invented' }]
    expect(groupsOf('R1', unknown, PALETTE)[0]?.phrase).toBe('invented')
  })
})
