import { LaunchButton } from '@/components/LaunchButton'

/**
 * How much of a corpus meaning search has actually read, and the way to finish it.
 *
 * Both search boxes rank what is already embedded and never wait on an embedding endpoint — an
 * interactive request that did would hit its 20-second timeout on any corpus worth searching (see
 * `llm/semantic.rank`). So on a cold or half-warm corpus the meaning half is legitimately partial,
 * and this is the component that says so and offers to fix it.
 *
 * **The distinction this exists to draw.** Before it, the panels rendered one string for two
 * unrelated states: an unreachable Ollama and a corpus that had merely not been embedded yet both
 * arrived as `semantic_status`, both were labelled *"Meaning search off."*, and — worse — both
 * caused the hits that had already been computed to be dropped on the floor. A working search over
 * 600 blocks looked exactly like a broken one. The server now reports coverage as two numbers that
 * cannot be mistaken for a failure, and this renders each state as itself:
 *
 * - `status` set — the search could not run. Off, and worth the warning colour.
 * - `searched < total` — it ran over part of the corpus. A chore, not a fault, so it reads as an
 *   offer rather than an error, and the results above it stay on screen where they belong.
 * - otherwise — complete. Nothing to say, so nothing is said.
 *
 * The finishing pass goes through `LaunchButton` like every other spend: a plan first (how many
 * embeddings, on which backend, whether it bills), then a click, then a bar that moves and a cancel
 * that works. Cancelling costs only the remainder — every batch is on disk before the next starts,
 * so the next launch resumes rather than restarts. The panel above refills on its own when the pass
 * lands: `onJobSettled` invalidates both search queries for a `meaning` job, so the refetch happens
 * wherever the operator is rather than only in the component that launched it.
 */
export function MeaningCoverage({
  skillId,
  scope,
  status,
  searched,
  total,
  unit,
}: {
  skillId: string
  /** Which corpus: the skill's own guidance, or the `.agents/` notes its role binds to. */
  scope: 'guidance' | 'sidecars'
  status: string
  searched: number
  total: number
  /** Singular noun for the things being embedded — "guidance block", "claim". */
  unit: string
}) {
  if (status) {
    return (
      <p className="text-sm text-muted">
        <span className="text-warn">Meaning search off.</span> {status}
      </p>
    )
  }
  if (total === 0 || searched >= total) return null

  const pct = Math.round((searched / total) * 100)
  const remaining = total - searched

  return (
    <div className="rounded-lg border border-line bg-surface p-3">
      <p className="text-sm text-muted">
        Meaning search has read{' '}
        <span className="text-ink tabular">
          {searched.toLocaleString()} of {total.toLocaleString()}
        </span>{' '}
        {unit}
        {total === 1 ? '' : 's'}
        {searched === 0
          ? ' — none of this corpus has been embedded yet, so the rows above are the exact matches alone.'
          : ' — anything in the rest that means something close has not been compared yet.'}
      </p>
      <div className="mt-2 h-1 overflow-hidden rounded-full bg-line">
        <div
          className="h-full bg-accent transition-[width] duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-2.5">
        <LaunchButton
          kind="meaning"
          request={{ skill_id: skillId, scope }}
          label={`Read the remaining ${remaining.toLocaleString()} ${unit}${remaining === 1 ? '' : 's'}`}
        />
      </div>
    </div>
  )
}
