import { useJudge } from '@/api/client'
import { Badge, Empty, ErrorNote, Intro, Loading, Metric } from '@/components/primitives'

/**
 * The judge is the instrument every score is computed with — recall, fp rate, gate verdicts are
 * all aggregations of its match/no-match calls. This page answers the questions an operator has
 * exactly when trust is at stake: what doctrine is the judge running, under what identity (so a
 * re-baselined trend line is explainable), and how much labeled evidence has accumulated toward
 * measuring it.
 */
export function JudgePage() {
  const { data, isLoading, error } = useJudge()

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-lg font-semibold">Judge</h1>
        <code className="font-mono text-sm text-muted">{data.id}</code>
        {data.builtin ? (
          <Badge tone="neutral" title="No JUDGE.md found — running the doctrine shipped with Whetstone">
            built-in
          </Badge>
        ) : (
          <Badge tone="accent" title={`Loaded from ${data.path}`}>
            v{data.version}
          </Badge>
        )}
      </div>

      <Intro>
        Every score in this console is an aggregation of this judge's verdicts: it decides whether
        a reviewer finding and an eval case expectation describe the same underlying issue. Its
        identity hash is recorded on every run, so scores from different judges are never read as
        one series. Changing the doctrine below changes the instrument — edit{' '}
        <code className="font-mono">{data.path}</code>, and the next run records the new identity.
      </Intro>

      <div className="mt-3 flex flex-wrap gap-2">
        <Metric label="rulings collected" value={String(data.rulings_total)} />
        <Metric label="judge overruled" value={String(data.rulings_overruled)} />
      </div>
      <p className="mt-1 text-xs text-muted">
        Rulings are minted from run drill-downs — every “same issue / different issue” click on a
        verdict becomes a labeled pair. They are the judge's own eval corpus; measuring accuracy
        against it (and gating doctrine changes on that accuracy) is the judge-eval job, which is
        not built yet. Until then, collect rulings: the more that accumulate, the more a future
        accuracy number will mean.
      </p>

      <h2 className="mt-6 mb-2 text-xs tracking-wide text-muted uppercase">
        Doctrine
        <code className="ml-3 font-mono normal-case">{data.hash.slice(0, 12)}</code>
      </h2>
      <div className="rounded-lg border border-line bg-surface px-4 py-3 text-sm whitespace-pre-wrap">
        {data.system}
      </div>
    </div>
  )
}
