import { describe, expect, it } from 'vitest'
import { CROWDED, tooManyToRead } from '@/components/graph/Canvas'
import { colourOf, edgeStyleOf, hollowColour, type GraphPalette } from '@/components/graph/types'

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
  })

  it('resolves the hollow colour through the kind that names it', () => {
    // So the canvas can outline a missing node in the graph's own "broken" colour without having an
    // opinion about which kind that is.
    expect(hollowColour(PALETTE)).toBe('var(--color-bad)')
    expect(hollowColour({ ...PALETTE, colour: {} })).toBe('var(--color-bad)')
  })
})

describe('when a picture stops being readable', () => {
  it('is a threshold, not a hard limit', () => {
    // The server still returns the nodes and the list below the picture is still exact. What changes
    // is that the panel says so instead of presenting a texture as an answer.
    expect(tooManyToRead(CROWDED)).toBe(false)
    expect(tooManyToRead(CROWDED + 1)).toBe(true)
    expect(tooManyToRead(0)).toBe(false)
  })

  it('is crossed by a real broad query on a real multi-page skill', () => {
    // `issue:true` on a 12-page skill draws ~150 nodes. That case is why this exists: the first
    // version drew the blob silently, and a blob teaches the reader that the graph is useless.
    expect(tooManyToRead(150)).toBe(true)
  })
})
