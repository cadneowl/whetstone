import { describe, expect, it } from 'vitest'
import {
  narrowedCases,
  selectionFrom,
  selectionParam,
  sharpenWording,
} from './ImproveWorkspace'

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

/**
 * What the sharpen button promises. The panel above it says "the checkboxes drive what gets scored,
 * sharpened and gate-targeted" — so a button that ignores the selection, or claims a narrowing the
 * request does not carry, contradicts the control the operator just used.
 */
describe('sharpenWording', () => {
  it('names the selection on both paths, batch or not', () => {
    // The no-batch path is the common one and used to hardcode "Draft from the last run" /
    // "Drafts from every case the last run failed" while sending `cases` all the same.
    expect(sharpenWording(['a'], 1, false)).toEqual({
      label: 'Draft from selected',
      scope: 'Drafts from the 1 selected case(s)',
    })
    expect(sharpenWording(['a', 'b'], 2, true)).toEqual({
      label: 'Improve from selected',
      scope: 'Drafts from the 2 selected case(s)',
    })
  })

  it('does not claim a narrowing when everything is ticked', () => {
    // `narrowedCases` reads a full tick list as "no filter", which is the state a fresh visit
    // starts in — so a tick count here would promise something the request does not carry.
    const { label, scope } = sharpenWording(null, 3, false)
    expect(label).toBe('Draft from the last run')
    expect(scope).toContain('Every case is ticked')
    expect(scope).toContain('every failure in the run')
  })

  it('tells an empty selection how to narrow, and a full one how to un-narrow', () => {
    expect(sharpenWording(null, 0, true).scope).toContain('tick cases above to narrow it')
    expect(sharpenWording(null, 3, true).scope).toContain('untick some to narrow it')
  })

  it('agrees with narrowedCases about which state it is in', () => {
    // The two are read off the same selection one line apart; if they ever disagree the button
    // describes a request other than the one it sends.
    for (const selected of [new Set<string>(), new Set(['a']), new Set(ALL)]) {
      const narrowed = narrowedCases(selected, ALL)
      const { scope } = sharpenWording(narrowed, selected.size, true)
      expect(scope.startsWith('Drafts from the')).toBe(narrowed !== null)
    }
  })
})
