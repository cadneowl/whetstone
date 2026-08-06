import { describe, expect, it } from 'vitest'
import type { Sweep, WatchState } from '@/api/client'
import { pullScope, sweepSummary } from './sweepSummary'

const AT = '2026-08-06T09:00:00Z'

function sweep(over: Partial<Sweep> = {}): Sweep {
  return {
    at: AT,
    projects: ['acme/payments'],
    found: 0,
    already_queued: 0,
    already_decided: 0,
    skipped: [],
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
