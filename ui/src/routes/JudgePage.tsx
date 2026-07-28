import { useQueryClient } from '@tanstack/react-query'
import { keys, useJudge } from '@/api/client'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Intro, Loading, Metric, when } from '@/components/primitives'

/**
 * The judge is the instrument every score is computed with — recall, fp rate, gate verdicts are
 * all aggregations of its match/no-match calls. This page answers the questions an operator has
 * exactly when trust is at stake: what doctrine is the judge running, under what identity (so a
 * re-baselined trend line is explainable), and how much labeled evidence has accumulated toward
 * measuring it.
 */
export function JudgePage() {
  const { data, isLoading, error } = useJudge()
  const queryClient = useQueryClient()

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  const measured = data.measured
  const passes = measured ? measured.accuracy >= data.bar : null

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
        {measured && (
          <>
            <Metric label="accuracy" value={measured.accuracy.toFixed(3)} />
            <Metric label="missed / spurious" value={`${measured.missed} / ${measured.spurious}`} />
            <Metric label="bar" value={data.bar.toFixed(3)} />
          </>
        )}
      </div>

      {measured ? (
        <p className="mt-1 text-xs">
          <span className={passes ? 'text-good' : 'text-bad'}>
            {passes ? 'Clears the bar.' : 'Below the bar.'}
          </span>{' '}
          <span className="text-muted">
            Measured {when(measured.at)} over {measured.total} pair(s)
            {measured.binding
              ? '.'
              : ' — too few pairs to move the bar; keep ruling on verdicts.'}{' '}
            <em>Spurious</em> is the number to watch: a spurious match reads as green while it
            quietly stops a case from discriminating.
          </span>
        </p>
      ) : (
        <p className="mt-1 text-xs text-muted">
          This doctrine has not been measured. Rulings are minted from run drill-downs — every
          “same issue / different issue” click becomes a labeled pair — and the measurement below
          scores the judge against all of them. The bar ratchets: once a judge demonstrates an
          accuracy over enough pairs, no later doctrine clears meaningfully below it.
        </p>
      )}

      {data.escalation && (
        <p className="mt-2 text-xs text-muted">
          <span className="font-medium text-ink">
            escalation rate {(data.escalation.rate * 100).toFixed(0)}%
          </span>{' '}
          — {data.escalation.escalated} of {data.escalation.verdicts} verdict(s) over the last{' '}
          {data.escalation.runs} run(s) were re-judged grounded in the case diff.{' '}
          {data.escalation.escalated === 0
            ? 'Zero usually means the cascade is off (escalate_below: 0 in evaluate/step.yaml).'
            : 'This is the number a distilled tier-1 judge has to keep honest: cheap verdicts take the bulk, the grounded teacher keeps the contested calls. `whetstone judge export` writes the training triples.'}
        </p>
      )}

      <div className="mt-3">
        <LaunchButton
          kind="judge-eval"
          request={{}}
          label="Measure the judge"
          disabled={data.pairs_total === 0}
          disabledReason="No labeled pairs yet — rule on judge verdicts in a run drill-down first."
          onDone={() => void queryClient.invalidateQueries({ queryKey: keys.judge })}
        />
      </div>

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
