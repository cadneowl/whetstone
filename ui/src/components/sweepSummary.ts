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
  const parts = [
    found > 0 ? `${found} new` : 'nothing new',
    (sweep.already_queued ?? 0) > 0 ? `${sweep.already_queued} already queued` : '',
    (sweep.already_decided ?? 0) > 0 ? `${sweep.already_decided} already ruled on` : '',
    (sweep.skipped ?? []).length > 0 ? `${(sweep.skipped ?? []).length} unreachable` : '',
  ].filter(Boolean)
  return { tone: found > 0 ? 'good' : 'muted', text: parts.join(' · ') }
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
