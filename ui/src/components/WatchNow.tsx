import { useCheckNow, useConsoleConfig, useWatch } from '@/api/client'
import { ErrorNote, when } from '@/components/primitives'
import { pullScope, sweepSummary } from '@/components/sweepSummary'

/**
 * Pull the watched projects now, and say what came back.
 *
 * One component for every screen that offers it, because the click is asynchronous and getting that
 * wrong is invisible: the request returns as soon as the sweep has *started*, so a screen reporting
 * the response as the result would show the previous sweep's numbers as though they were this
 * one's. Only the polled state knows, and `useWatch` is where that lives.
 *
 * The whole point of the button is the sweep the schedule will not do soon enough — an interval is
 * a promise about the average case, and "an open merge request landed two minutes ago and I am
 * triaging now" is not the average case. It is also the only way to seed a queue from the console:
 * before this, a fresh install had to be pulled from the command line once before the triage screen
 * had anything on it at all.
 */
export function WatchNow({ className = '' }: { className?: string }) {
  const { data: watch } = useWatch()
  const { data: config } = useConsoleConfig()
  const check = useCheckNow()

  const readOnly = Boolean(config?.read_only)
  // Its own sweep or the timer's — either way something is walking the forge and the answer is not
  // in yet. `isPending` covers the moment before the first poll has seen `polling` go true.
  const busy = check.isPending || Boolean(watch?.polling)
  const sweep = watch?.last_sweep ?? null
  const summary = sweep ? sweepSummary(sweep) : null
  const scope = pullScope(watch)

  return (
    <div className={`text-xs text-muted ${className}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <button
          type="button"
          disabled={busy || readOnly}
          onClick={() => check.mutate()}
          title={
            readOnly
              ? 'The console is running read-only, so it will not reach out to a forge. Restart ' +
                'without --read-only (or set [ui] read_only = false in whetstone.toml).'
              : 'Mine the watched projects for new signal right now — the same sweep the watcher ' +
                'runs on its interval, and the only one that does not make you wait for it.'
          }
          className="rounded border border-line px-2 py-0.5 transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted"
        >
          {busy ? 'Pulling…' : 'Pull now'}
        </button>
        <p>
          {watch?.enabled ? (
            <>Watching every {watch.interval_minutes} min</>
          ) : (
            <>
              Not watching — <span className="font-mono">[watch] enabled = true</span> in
              whetstone.toml turns it on
            </>
          )}
          {scope && <> · {scope}</>}
        </p>
      </div>

      {/* Shown whether or not anything is watching on a schedule: `Pull now` runs a real sweep
          either way, and this used to sit inside the `enabled` branch — so on the far commoner
          setup, where watching is off, clicking it reached out to a forge and reported absolutely
          nothing back. A button indistinguishable from a broken one. */}
      {check.error ? (
        <div className="mt-2">
          <ErrorNote error={check.error} />
        </div>
      ) : (
        summary &&
        sweep &&
        !busy && (
          <p
            className={`mt-1.5 ${
              summary.tone === 'bad'
                ? 'rounded border border-bad/40 bg-bad/5 px-2 py-1 text-bad'
                : summary.tone === 'good'
                  ? 'text-accent'
                  : ''
            }`}
          >
            {summary.tone === 'bad' ? 'Pull failed' : 'Pulled'} {when(sweep.at)} · {summary.text}
          </p>
        )
      )}
    </div>
  )
}
