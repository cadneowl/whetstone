import { describe, expect, it } from 'vitest'
import type { PendingCase } from '@/api/client'
import { isFailing, narrowedCases, selectionFrom } from './ImproveWorkspace'

/**
 * Sharpening a promoted case routinely breaks something already in the corpus. That regression is
 * the whole reason the "…with the eval corpus too" button exists — and it was visible and then
 * unactionable: the improve request was pinned to the promoted selection, so a graduated case could
 * never be drafted from. When the promoted case had meanwhile started passing, the step refused
 * outright, advising the operator to "pick a case the last run failed" while offering no such case
 * to pick.
 */
function pendingCase(over: Partial<PendingCase> = {}): PendingCase {
  return {
    id: 'promoted-1',
    kind: 'should_catch',
    path: 'src/A.java',
    provenance: {},
    last_recall: 1,
    last_fp_rate: 0,
    holdout: false,
    ...over,
  } as PendingCase
}

describe('isFailing', () => {
  it('calls a catch case failing when it was missed', () => {
    expect(isFailing(pendingCase({ last_recall: 0 }))).toBe(true)
    expect(isFailing(pendingCase({ last_recall: 1 }))).toBe(false)
  })

  it('calls a no-flag case failing when it was flagged', () => {
    const noflag = { kind: 'should_not_flag' as const, last_recall: null }
    expect(isFailing(pendingCase({ ...noflag, last_fp_rate: 0.5 }))).toBe(true)
    expect(isFailing(pendingCase({ ...noflag, last_fp_rate: 0 }))).toBe(false)
  })

  it('does not call an unscored case failing — no evidence is not bad evidence', () => {
    expect(isFailing(pendingCase({ last_recall: null }))).toBe(false)
  })

  it('reads a graduated case the same way, which is what makes one selectable', () => {
    // `CaseSummary` carries the same outcome fields as `PendingCase`; the workspace filters the
    // graduated corpus with this exact predicate to find what its own work has broken.
    const graduated = { id: 'corpus-1', kind: 'should_catch', last_recall: 0, last_fp_rate: 0 }
    expect(isFailing(graduated as PendingCase)).toBe(true)
  })
})

describe('narrowedCases', () => {
  const all = ['promoted-1', 'corpus-broke-1', 'corpus-broke-2']

  it('sends nothing when everything is ticked, so every failure stays in scope', () => {
    // The state a fresh visit starts in. Reading it as "draft from exactly these" is the bug: with
    // only the batch ticked, a corpus regression in the same run was silently dropped.
    expect(narrowedCases(new Set(all), all)).toBeNull()
  })

  it('sends nothing when nothing is ticked', () => {
    expect(narrowedCases(new Set(), all)).toBeNull()
  })

  it('narrows to a strict subset, in list order', () => {
    expect(narrowedCases(new Set(['corpus-broke-2', 'promoted-1']), all)).toEqual([
      'promoted-1',
      'corpus-broke-2',
    ])
  })

  it('lets a corpus regression be the only thing drafted from', () => {
    // The case that could not be expressed at all before: the promoted batch passes, something
    // graduated broke, and that break is what needs sharpening.
    expect(narrowedCases(new Set(['corpus-broke-1']), all)).toEqual(['corpus-broke-1'])
  })
})

describe('selectionFrom over the combined list', () => {
  const all = ['promoted-1', 'corpus-broke-1']

  it('defaults to everything, promoted and graduated alike', () => {
    expect(selectionFrom(null, all)).toEqual(new Set(all))
  })

  it('keeps a graduated id from a shared link', () => {
    expect(selectionFrom('corpus-broke-1', all)).toEqual(new Set(['corpus-broke-1']))
  })

  it('drops an id that is in neither list rather than sending it to the server', () => {
    expect(selectionFrom('corpus-broke-1,graduated-since', all)).toEqual(
      new Set(['corpus-broke-1']),
    )
  })
})
