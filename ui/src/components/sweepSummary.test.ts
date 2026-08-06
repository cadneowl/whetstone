import { describe, expect, it } from 'vitest'
import type { Sweep, WatchState } from '@/api/client'
import { localDay, pullScope, startOfDay, sweepSummary } from './sweepSummary'

const AT = '2026-08-06T09:00:00Z'

function sweep(over: Partial<Sweep> = {}): Sweep {
  return {
    at: AT,
    projects: ['acme/payments'],
    found: 0,
    already_queued: 0,
    already_decided: 0,
    skipped: [],
    rewound: [],
    backfill_from: null,
    error: '',
    duration_s: 1.2,
    ...over,
  }
}

function watch(over: Partial<WatchState> = {}): WatchState {
  return {
    enabled: true,
    interval_minutes: 30,
    include_open: false,
    polling: false,
    since: {},
    last_sweep: null,
    next_sweep_at: null,
    history: [],
    ...over,
  }
}

describe('sweepSummary', () => {
  it('leads with what arrived', () => {
    expect(sweepSummary(sweep({ found: 3 }))).toEqual({ tone: 'good', text: '3 new' })
  })

  it('says nothing new rather than going blank', () => {
    // A blank line beside a button is indistinguishable from a button that did not run.
    expect(sweepSummary(sweep())).toEqual({ tone: 'muted', text: 'nothing new' })
  })

  it('separates what it re-found from what it found', () => {
    const summary = sweepSummary(sweep({ found: 1, already_queued: 4, already_decided: 2 }))
    expect(summary.text).toBe('1 new · 4 already queued · 2 already ruled on')
  })

  it('counts merge requests it could not reach', () => {
    const summary = sweepSummary(sweep({ skipped: ['acme/payments!7', 'acme/payments!9'] }))
    expect(summary.text).toBe('nothing new · 2 unreachable')
  })

  it('leads with the window when somebody chose it', () => {
    // "nothing new" over today and "nothing new" since March are not the same report, and the
    // numbers after it mean nothing without knowing which one this is.
    //
    // Round-tripped through the same pair the console uses, so this holds in every zone. Asserting
    // on a hard-coded `Z` instant would pass in UTC and report the day before in New York.
    const summary = sweepSummary(sweep({ backfill_from: startOfDay('2026-08-01'), found: 4 }))
    expect(summary.text).toBe('since 2026-08-01 · 4 new')
  })

  it('says nothing about a window nobody chose', () => {
    expect(sweepSummary(sweep({ found: 4 })).text).toBe('4 new')
  })

  it('names the project it re-walked, and why', () => {
    // A sweep that suddenly takes minutes and returns thirty candidates reads as a malfunction
    // unless it says what changed.
    const summary = sweepSummary(sweep({ found: 30, rewound: ['acme/payments'] }))
    expect(summary.text).toContain('re-walked acme/payments')
    expect(summary.text).toContain('widened')
  })

  it('counts the projects rather than listing them when there is more than one', () => {
    const summary = sweepSummary(sweep({ rewound: ['acme/payments', 'acme/ledger'] }))
    expect(summary.text).toContain('re-walked 2 projects')
  })

  it('reports the reason a sweep failed, not that it failed', () => {
    // "Check failed" beside an empty queue is the same screen as "nothing to find" — and one of
    // them means the token expired.
    const summary = sweepSummary(sweep({ error: 'ConnectorError: gitlab said 401' }))
    expect(summary).toEqual({ tone: 'bad', text: 'ConnectorError: gitlab said 401' })
  })

  it('reports the failure even when the same sweep found something first', () => {
    // A sweep can write one project's candidates and then fail on the next; the count is real, but
    // the window it did not cover is the thing to act on.
    expect(sweepSummary(sweep({ found: 2, error: 'HTTP 500' })).tone).toBe('bad')
  })
})

describe('the day an operator picked', () => {
  it('starts at midnight where they are, not where UTC is', () => {
    // The whole reason: the server refuses a pull from the future by comparing instants, and at
    // UTC+14 a bare day read as UTC midnight is still hours away — so the picker's own default
    // would be refused, on the one control somebody reached for because nothing else worked.
    const start = new Date(startOfDay('2026-08-01'))
    expect(start.getFullYear()).toBe(2026)
    expect(start.getMonth()).toBe(7)
    expect(start.getDate()).toBe(1)
    expect(start.getHours()).toBe(0)
  })

  it('comes back as the same day it went in', () => {
    for (const day of ['2026-01-01', '2026-08-01', '2026-12-31']) {
      expect(localDay(startOfDay(day))).toBe(day)
    }
  })

  it('pads a single-digit month and day, as the date input requires', () => {
    expect(localDay(new Date(2026, 0, 5))).toBe('2026-01-05')
  })
})

describe('pullScope', () => {
  it('names the setting when open merge requests are being skipped', () => {
    expect(pullScope(watch())).toContain('include_open')
  })

  it('says so when they are not', () => {
    expect(pullScope(watch({ include_open: true }))).toBe('open and merged merge requests')
  })

  it('says nothing before the state has loaded', () => {
    expect(pullScope(undefined)).toBe('')
  })
})
