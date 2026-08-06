// @vitest-environment jsdom
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { keys, useWatch, type Sweep, type WatchState } from '@/api/client'

/**
 * `useWatch` decides when a sweep has changed the world underneath whoever is looking at it.
 *
 * It is the only hook in the console that invalidates on its own — a sweep rewrites the triage
 * queue without anything on this side asking it to — and the failure mode is silence: the queue on
 * screen is stale, nothing is red, and the sweep line above it cheerfully reports two new
 * candidates. Both of the bugs below shipped and were found by clicking, which is what this file
 * exists to stop.
 *
 * Observations are pushed in with `setQueryData` rather than driven through the poll, because the
 * question is what the hook does with a sequence of states, not whether react-query can keep time.
 */

const AT = '2026-08-06T10:43:00Z'
const LATER = '2026-08-06T11:13:00Z'

function watch(over: Partial<WatchState> = {}): WatchState {
  return {
    enabled: false,
    interval_minutes: 30,
    include_open: true,
    polling: false,
    since: {},
    last_sweep: null,
    next_sweep_at: null,
    history: [],
    ...over,
  }
}

function sweep(at: string): Sweep {
  return {
    at,
    projects: ['acme/payments'],
    found: 2,
    already_queued: 0,
    already_decided: 0,
    skipped: [],
    error: '',
    duration_s: 4.1,
  }
}

/** A watcher that has never pulled — a console someone has just started. */
const never = () => watch()
/** A sweep in flight, which is what `Pull now` produces and what the timer produces. */
const sweeping = () => watch({ polling: true })
/** That sweep, landed. */
const landed = (at: string) => watch({ last_sweep: sweep(at) })

function harness() {
  const client = new QueryClient({
    // No refetching of its own: every observation in these tests is one this file put there.
    defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
  })
  const invalidated: readonly unknown[][] = []
  const calls = invalidated as unknown[][]
  vi.spyOn(client, 'invalidateQueries').mockImplementation(((filters?: {
    queryKey?: unknown[]
  }) => {
    calls.push(filters?.queryKey ?? [])
    return Promise.resolve()
  }) as typeof client.invalidateQueries)

  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  const observe = (state: WatchState) => act(() => void client.setQueryData(keys.watch, state))
  return { client, wrapper, invalidated, observe }
}

/** What a stale queue looks like from here: the queue and the home screen, in that order. */
const REFRESHED = [keys.candidates, keys.inbox]

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('useWatch', () => {
  it('refreshes the queue when a sweep lands on a console that has never pulled', async () => {
    // The case the button exists for, and the one the first version of this got wrong: with no
    // sweep on record there was nothing it would call a baseline, so the pull that filled the queue
    // never invalidated it. The screen said "nothing mined yet" under "Pulled … · 2 new".
    const { client, wrapper, invalidated, observe } = harness()
    client.setQueryData(keys.watch, never())
    renderHook(() => useWatch(), { wrapper })
    await waitFor(() => expect(invalidated).toEqual([]))

    observe(sweeping())
    expect(invalidated).toEqual([])

    observe(landed(AT))
    await waitFor(() => expect(invalidated).toEqual(REFRESHED))
  })

  it('refreshes when a sweep already running at mount lands', async () => {
    // Opening triage while the timer is mid-sweep: the queue on screen predates that sweep, so what
    // it brings in is news. Treating "in flight" as nothing-to-compare made its result the baseline
    // instead, and the candidates it mined never appeared.
    const { client, wrapper, invalidated, observe } = harness()
    client.setQueryData(keys.watch, sweeping())
    renderHook(() => useWatch(), { wrapper })
    await waitFor(() => expect(invalidated).toEqual([]))

    observe(landed(AT))
    await waitFor(() => expect(invalidated).toEqual(REFRESHED))
  })

  it('leaves the queue alone for a sweep that happened before the screen opened', async () => {
    // Otherwise every navigation to triage refetches the whole queue to discover nothing changed.
    const { client, wrapper, invalidated } = harness()
    client.setQueryData(keys.watch, landed(AT))
    renderHook(() => useWatch(), { wrapper })

    await waitFor(() => expect(invalidated).toEqual([]))
  })

  it('leaves the queue alone when a poll re-reads the same sweep', async () => {
    const { client, wrapper, invalidated, observe } = harness()
    client.setQueryData(keys.watch, landed(AT))
    renderHook(() => useWatch(), { wrapper })

    observe(landed(AT))
    await waitFor(() => expect(invalidated).toEqual([]))
  })

  it('refreshes once per sweep, not once per start and finish', async () => {
    // `Pull now` twice in a row: the queue is re-read when each lands, and never in between —
    // a sweep *starting* has changed nothing yet.
    const { client, wrapper, invalidated, observe } = harness()
    client.setQueryData(keys.watch, landed(AT))
    renderHook(() => useWatch(), { wrapper })

    observe(sweeping())
    expect(invalidated).toEqual([])

    observe(landed(LATER))
    await waitFor(() => expect(invalidated).toEqual(REFRESHED))
  })
})
