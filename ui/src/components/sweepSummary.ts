import type { Sweep, WatchState } from '@/api/client'

/**
 * What one sweep of the watched projects did, in the terms an operator would ask it in.
 *
 * Pulled out of the three screens that show it because the interesting part is not the layout: a
 * sweep that found nothing and a sweep that failed are the same shape on the wire, and reporting
 * the second as the first is how a queue stays empty for a week without anybody noticing that the
 * token expired.
 */
export interface SweepSummary {
  tone: 'good' | 'bad' | 'muted'
  text: string
}

export function sweepSummary(sweep: Sweep): SweepSummary {
  // The reason, never a bare "the last check failed". An expired token and a project nobody
  // configured need different things doing about them.
  if (sweep.error) return { tone: 'bad', text: sweep.error }

  const found = sweep.found ?? 0
  const rewound = sweep.rewound ?? []
  const parts = [
    // The window first when somebody chose it, because it changes what every number after it means:
    // "nothing new" over today and "nothing new" since March are not the same report. Rendered on
    // the reader's calendar, so it echoes the day they picked rather than the UTC instant it became.
    sweep.backfill_from ? `since ${localDay(sweep.backfill_from)}` : '',
    found > 0 ? `${found} new` : 'nothing new',
    (sweep.already_queued ?? 0) > 0 ? `${sweep.already_queued} already queued` : '',
    (sweep.already_decided ?? 0) > 0 ? `${sweep.already_decided} already ruled on` : '',
    (sweep.skipped ?? []).length > 0 ? `${(sweep.skipped ?? []).length} unreachable` : '',
    // Why this one sweep took minutes when the last hundred took seconds, and why a queue that had
    // been stable suddenly has thirty things in it. Left unsaid, a re-walk reads as a malfunction.
    rewound.length > 0
      ? `re-walked ${rewound.length === 1 ? rewound[0] : `${rewound.length} projects`} ` +
        'from the start of the lookback — what is being mined widened'
      : '',
  ].filter(Boolean)
  return { tone: found > 0 ? 'good' : 'muted', text: parts.join(' · ') }
}

/**
 * A day on the reader's own calendar, as `<input type="date">` spells it.
 *
 * Their day, never UTC's. This caps a date picker somebody is reading off the wall, and labels the
 * window a sweep covered; `toISOString().slice(0, 10)` would put everyone east or west of UTC a day
 * out of step with both.
 */
export function localDay(when: Date | string = new Date()): string {
  const d = typeof when === 'string' ? new Date(when) : when
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/**
 * A picked day as the instant it starts — midnight where the operator is.
 *
 * The server compares this against the wall clock to refuse a pull from the future, so a bare day
 * read as UTC midnight refuses the picker's own default for everyone ahead of UTC: at UTC+14,
 * "today" does not begin until UTC has another fourteen hours to run. It is also simply what was
 * meant — "everything since the 1st" means since the 1st began *here*.
 */
export function startOfDay(day: string): string {
  // No `Z`, so this parses in the browser's own zone. The `T00:00:00` matters: a bare `2026-08-01`
  // is specified to parse as UTC, which is the bug this function exists to avoid.
  return new Date(`${day}T00:00:00`).toISOString()
}

/**
 * What a pull will and will not look at.
 *
 * Shown beside the button because it is the answer to the question an empty queue always raises —
 * *should* this have found anything? A sweep that mines merged history only and one that also reads
 * open merge requests produce very different queues from the same projects, and nothing in the
 * result tells the two apart: both are just a number of candidates.
 */
export function pullScope(watch: WatchState | undefined): string {
  if (!watch) return ''
  return watch.include_open
    ? 'open and merged merge requests'
    : 'merged merge requests only — [watch] include_open = true adds open ones'
}
