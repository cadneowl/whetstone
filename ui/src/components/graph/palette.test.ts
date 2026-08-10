import { describe, expect, it } from 'vitest'
import { CROWDED, tooManyToRead } from '@/components/graph/Canvas'
import {
  colourOf,
  edgeStyleOf,
  hollowColour,
  relationOf,
  type GraphPalette,
} from '@/components/graph/types'

/**
 * The kind-agnostic half of the graph canvas.
 *
 * One canvas now serves two graphs that agree about nothing except the shape of the picture, so the
 * lookups have to survive a graph growing a kind its palette has not learnt — silently drawing an
 * invisible node would be the worst outcome, because a missing dot reads as an absent fact.
 */

const PALETTE: GraphPalette = {
  colour: { file: 'var(--color-accent)', unresolved: 'var(--color-bad)' },
  help: { file: 'a guidance file' },
  hollow: 'unresolved',
  anchors: ['file'],
  edge: { links: { opacity: 0.85, dash: '5 3' } },
  edgeHelp: { links: 'a link an author wrote' },
  edgeRelation: { links: { out: 'links to', in: 'linked from' } },
}

describe('palette lookups', () => {
  it('answers for a kind it knows', () => {
    expect(colourOf(PALETTE, 'file')).toBe('var(--color-accent)')
    expect(edgeStyleOf(PALETTE, 'links')).toEqual({ opacity: 0.85, dash: '5 3' })
  })

  it('falls back rather than returning undefined for one it does not', () => {
    // `noUncheckedIndexedAccess` makes the miss visible at the type level; these are the values that
    // keep an unknown kind *drawn* rather than invisible, which is what a reader would misread.
    expect(colourOf(PALETTE, 'invented')).toBe('var(--color-muted)')
    expect(edgeStyleOf(PALETTE, 'invented')).toEqual({ opacity: 0.4 })
    expect(relationOf(PALETTE, 'invented', true)).toBe('invented')
  })

  it('reads a known edge from either end', () => {
    // The two readings are different sentences, and the neighbour list under the picture needs the
    // one that matches which end the reader is standing at.
    expect(relationOf(PALETTE, 'links', true)).toBe('links to')
    expect(relationOf(PALETTE, 'links', false)).toBe('linked from')
  })

  it('resolves the hollow colour through the kind that names it', () => {
    // So the canvas can outline a missing node in the graph's own "broken" colour without having an
    // opinion about which kind that is.
    expect(hollowColour(PALETTE)).toBe('var(--color-bad)')
    expect(hollowColour({ ...PALETTE, colour: {} })).toBe('var(--color-bad)')
  })
})

describe('when a picture stops being readable', () => {
  it('judges connectedness before size', () => {
    // The failure that motivated this: a broad query matched more nodes than the limit allows, so
    // `hops` added no neighbours and the drawing was *edgeless* — which force layout renders as a
    // ring of dots round the boundary. Node count alone would have missed a sparse 50.
    expect(tooManyToRead(400, 3)).toBe(true)
    expect(tooManyToRead(50, 4)).toBe(true)
  })

  it('leaves a well-connected drawing alone', () => {
    // 73 nodes with 73 edges draws as legible clusters on a real skill. The first version warned
    // about it anyway, which is the same crying-wolf mistake as flagging generic guidance.
    expect(tooManyToRead(73, 73)).toBe(false)
    expect(tooManyToRead(60, 40)).toBe(false)
  })

  it('gives up past a hard size whatever the connectedness', () => {
    expect(tooManyToRead(CROWDED, CROWDED * 2)).toBe(false)
    expect(tooManyToRead(CROWDED + 1, CROWDED * 2)).toBe(true)
  })

  it('says nothing about a handful of dots', () => {
    expect(tooManyToRead(0, 0)).toBe(false)
    expect(tooManyToRead(12, 0)).toBe(false)
  })
})
