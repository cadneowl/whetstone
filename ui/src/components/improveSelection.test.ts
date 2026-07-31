import { describe, expect, it } from 'vitest'
import { selectionFrom, selectionParam } from './ImproveWorkspace'

/**
 * The Improve workspace keeps its case selection in the URL so a trip to the Edit tab — which step
 * 2 explicitly sends you on — comes back to the same workspace. That only works if three states
 * survive the round trip, and two of them look alike: "all" (the default) and "none" (a deliberate
 * choice) are both round numbers, but they drive different requests. All selected scores the whole
 * batch; none selected drafts from every failure and targets nothing in the gate.
 */
const ALL = ['a', 'b', 'c']

describe('selectionFrom', () => {
  it('treats an absent param as everything, so a fresh visit starts selected', () => {
    expect([...selectionFrom(null, ALL)]).toEqual(ALL)
  })

  it('round-trips a subset', () => {
    expect([...selectionFrom('a,c', ALL)]).toEqual(['a', 'c'])
  })

  it('keeps an empty selection distinct from an absent one', () => {
    expect([...selectionFrom('-', ALL)]).toEqual([])
    expect([...selectionFrom('', ALL)]).toEqual([])
  })

  it('drops ids that are no longer pending', () => {
    // A stale link, or a case graduated into the eval corpus since. Carrying it into the request
    // would have the server refuse a gate for naming a case it cannot score.
    expect([...selectionFrom('a,graduated,c', ALL)]).toEqual(['a', 'c'])
  })

  it('survives a corpus that has emptied out', () => {
    expect([...selectionFrom('a,b', [])]).toEqual([])
    expect([...selectionFrom(null, [])]).toEqual([])
  })
})

describe('selectionParam', () => {
  it('leaves the param off when everything is selected', () => {
    expect(selectionParam(new Set(ALL), ALL)).toBeNull()
  })

  it('writes a sentinel for none, never an absent param', () => {
    expect(selectionParam(new Set(), ALL)).toBe('-')
  })

  it('writes a subset in corpus order, so the URL is stable however it was clicked', () => {
    expect(selectionParam(new Set(['c', 'a']), ALL)).toBe('a,c')
    expect(selectionParam(new Set(['a', 'c']), ALL)).toBe('a,c')
  })
})

describe('the round trip', () => {
  it.each([
    ['all', new Set(ALL)],
    ['none', new Set<string>()],
    ['one', new Set(['b'])],
    ['two', new Set(['a', 'c'])],
  ])('preserves %s', (_label, selected) => {
    expect([...selectionFrom(selectionParam(selected, ALL), ALL)]).toEqual(
      ALL.filter((id) => selected.has(id)),
    )
  })
})
