import { describe, expect, it } from 'vitest'
import type { ExpectationOutcome, Finding, TrialRecord } from '@/api/client'
import { excludedFindings } from './RunDetail'

/**
 * The drill-down explains why a case failed, so it must not contradict the score it is explaining.
 * Eligibility runs against `considered` — the expectation's anchor widened to what the change
 * touches — and a page that recomputed it from `where` would print "outside the expected line
 * range" beside a finding the run had accepted, which is how an operator concludes the numbers are
 * arbitrary and stops trusting them.
 */
const PATH = 'risk/App.java'

function finding(line: number, path = PATH): Finding {
  return { skill_id: 's', path, line, severity: 30, message: 'wrong exception type' } as Finding
}

function outcome(over: Partial<ExpectationOutcome> = {}): ExpectationOutcome {
  return {
    expectation_id: 'e1',
    must: 'appear',
    outcome: 'fn',
    semantic: 'x',
    where: { path: PATH, line_range: [73, 73] },
    considered: { path: PATH, line_range: [72, 91] },
    eligible_finding_indices: [],
    verdicts: [],
    ...over,
  } as ExpectationOutcome
}

function trial(findings: Finding[]): TrialRecord {
  return { index: 0, findings, outcomes: [] } as TrialRecord
}

describe('excludedFindings', () => {
  it('does not call a finding out of range when the run judged it', () => {
    const findings = [finding(82)]

    const out = excludedFindings(outcome({ eligible_finding_indices: [0] }), trial(findings))

    expect(out).toEqual([])
  })

  it('still explains a finding outside everything the change touches', () => {
    const out = excludedFindings(outcome(), trial([finding(400)]))

    expect(out).toEqual([{ index: 0, reason: 'outside the lines this change touches' }])
  })

  it('reads an old record with no considered region exactly as it ran', () => {
    // Records written before the widening carry only `where`. Falling back to it keeps their
    // explanation true to the exact-line rule those runs were actually scored under.
    const out = excludedFindings(outcome({ considered: null }), trial([finding(82)]))

    expect(out).toEqual([{ index: 0, reason: 'outside the lines this change touches' }])
  })

  it('leaves other files alone — they belong to no expectation here', () => {
    const out = excludedFindings(outcome(), trial([finding(82, 'other/File.java')]))

    expect(out).toEqual([])
  })

  it('blames severity when the finding is inside the region', () => {
    const out = excludedFindings(outcome(), trial([finding(80)]))

    expect(out).toEqual([{ index: 0, reason: 'below the required severity' }])
  })
})
