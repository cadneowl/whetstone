import { describe, expect, it } from 'vitest'

import { summaryOf } from './LaunchButton'

describe('summaryOf', () => {
  it('reads the summary a finished job carries', () => {
    const s = summaryOf({
      summary: {
        verdict: 'failed',
        headline: 'FAILED — recall 0.333 → 0.833, 1 reason(s) below',
        reasons: ['1 case(s) regressed (max 0): a\n  · a: the baseline passed it'],
        caveats: ['every case was measured once on each side (k=1)'],
      },
    })
    expect(s?.verdict).toBe('failed')
    expect(s?.reasons).toHaveLength(1)
    expect(s?.caveats).toHaveLength(1)
  })

  it('returns null for a result from a server that sends none', () => {
    // Version skew, not a defect — the caller renders nothing rather than an empty verdict box
    // that reads as "this run declined to explain itself".
    expect(summaryOf({ passed: true, recall_new: 1 })).toBeNull()
  })

  it('refuses a summary with no headline, which is the one part that must stand alone', () => {
    expect(summaryOf({ summary: { verdict: 'passed', reasons: [] } })).toBeNull()
  })

  it('survives lists that are not lists of strings', () => {
    const s = summaryOf({
      summary: { headline: 'ok', reasons: ['real', 7, null], caveats: 'not a list' },
    })
    expect(s?.reasons).toEqual(['real'])
    expect(s?.caveats).toEqual([])
  })
})
