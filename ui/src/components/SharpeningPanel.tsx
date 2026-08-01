import { useSharpening, type ProvenFix, type TrendPoint } from '@/api/client'
import { Badge, Empty, ErrorNote, Loading, score, when } from '@/components/primitives'

/**
 * Is this skill actually getting sharper?
 *
 * The panel is deliberately ordered against the instinct it is answering. The **verdict** comes
 * first, then the **ledger** — cases a gate proved went from failing to passing — and only then the
 * chart. Leading with the chart would be leading with the weakest evidence, because the line moves
 * whenever the corpus, the judge or the model moves, and a healthy loop moves the corpus every
 * week. See `whetstone/sharpening.py` for the full argument.
 *
 * The seams are drawn, not annotated in a tooltip: the whole point is to interrupt the eye that was
 * about to read a rise as progress.
 */
export function SharpeningPanel({ skillId }: { skillId: string }) {
  const { data, isLoading, error } = useSharpening(skillId)
  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  // Every list here has a server-side default, so the generated schema marks it optional. Coerced
  // once at the top rather than guarded at each of a dozen use sites.
  const points = data.points ?? []
  const taskPoints = data.task_points ?? []
  const fixes = data.proven_fixes ?? []
  const regressions = data.regressions ?? []
  const caveats = data.caveats ?? []
  const demonstrable = data.verdict.startsWith('sharpening')

  return (
    <div className="space-y-6">
      <div
        className={`rounded-lg border px-4 py-3 ${
          demonstrable ? 'border-good/40 bg-good/5' : 'border-line bg-surface'
        }`}
      >
        <div className="text-[11px] tracking-wide text-muted uppercase">Verdict</div>
        <p className="mt-1 text-sm">{data.verdict}</p>
      </div>

      <section>
        <h3 className="mb-1 text-xs tracking-wide text-muted uppercase">
          The ledger — what a gate actually proved
        </h3>
        <p className="mb-3 max-w-3xl text-sm text-muted">
          A gate holds the case set, the judge and the reviewer fixed on both sides, so a case it
          recorded going from failing to passing genuinely improved. This is the strong evidence;
          the chart below is not.
        </p>
        <div className="mb-3 flex flex-wrap gap-2 text-sm">
          <Badge tone={fixes.length ? 'good' : 'neutral'}>
            {fixes.length} case(s) proven fixed
          </Badge>
          <Badge tone={data.fixes_that_stuck === fixes.length ? 'neutral' : 'warn'}>
            {data.fixes_that_stuck} still passing
          </Badge>
          <Badge tone={regressions.length ? 'bad' : 'neutral'}>
            {regressions.length} regressed at some point
          </Badge>
          <Badge tone="neutral">
            {data.gates_passed}/{data.gates_run} gate(s) passed
          </Badge>
          {data.gates_proving_nothing > 0 && (
            <Badge
              tone="warn"
              title="A passing gate that named no case it should fix, and fixed none: it proves nothing broke."
            >
              {data.gates_proving_nothing} proved nothing
            </Badge>
          )}
        </div>
        {fixes.length === 0 ? (
          <p className="text-sm text-muted italic">
            No gate has recorded a case going from failing to passing. Name the cases a change is
            meant to fix when you gate it — that is what turns "nothing broke" into evidence.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {fixes.map((fix) => (
              <FixRow key={fix.case_id} fix={fix} />
            ))}
          </ul>
        )}
        {regressions.length > 0 && (
          <p className="mt-3 text-sm">
            <span className="text-muted">Regressed at some point: </span>
            <span className="font-mono text-xs">{regressions.join(', ')}</span>
          </p>
        )}
      </section>

      {points.length > 0 && (
        <section>
          <h3 className="mb-1 text-xs tracking-wide text-muted uppercase">
            The trend — read for shape, not for a delta
          </h3>
          <p className="mb-3 max-w-3xl text-sm text-muted">
            {points.length < 2 ? (
              // "Every step crosses a change" is false when there are no steps. A single run is a
              // measurement, and saying so is different from saying the history is unreadable.
              <>One run so far — a delta needs two.</>
            ) : data.recall_delta === null ? (
              <>
                No delta is quotable: every step here crosses a change in what was being measured.
              </>
            ) : (
              <>
                Recall{' '}
                <span className={data.recall_delta >= 0 ? 'text-good' : 'text-bad'}>
                  {data.recall_delta >= 0 ? '+' : ''}
                  {data.recall_delta.toFixed(3)}
                </span>{' '}
                across the longest unbroken stretch ({data.comparable_runs} run(s)) — not across the
                whole history below.
              </>
            )}
          </p>
          <TrendChart points={points} />
          <ul className="mt-3 space-y-1">
            {[...points].reverse().map((point) => (
              <PointRow key={point.run_id} point={point} />
            ))}
          </ul>
        </section>
      )}

      {taskPoints.length > 0 && (
        <section>
          <h3 className="mb-2 text-xs tracking-wide text-muted uppercase">
            Task runs — work produced, graded by running it
          </h3>
          <ul className="space-y-1">
            {[...taskPoints].reverse().map((point) => (
              <li
                key={point.run_id}
                className="flex flex-wrap items-baseline gap-x-4 rounded-lg border border-line bg-surface px-3 py-2 text-sm"
              >
                <span className="text-muted">{when(point.created_at)}</span>
                <span className="tabular">pass {score(point.pass_rate, 2)}</span>
                <span className="tabular">mean {score(point.mean_score, 2)}</span>
                <span className="text-xs text-muted">{point.cases} case(s)</span>
                {point.verifier_changed && (
                  <Badge tone="warn" title="The same work graded by a different verify: is a different score">
                    grader changed
                  </Badge>
                )}
                {point.executor_changed && <Badge tone="warn">executor changed</Badge>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {caveats.length > 0 && (
        <section>
          <h3 className="mb-2 text-xs tracking-wide text-muted uppercase">
            What this does not show
          </h3>
          <ul className="space-y-2">
            {caveats.map((caveat, i) => (
              <li
                key={i}
                className="rounded-lg border border-warn/30 bg-warn/5 px-3 py-2 text-sm text-muted"
              >
                {caveat}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

function FixRow({ fix }: { fix: ProvenFix }) {
  const state =
    fix.still_holds === true
      ? { tone: 'good' as const, label: 'still passes' }
      : fix.still_holds === false
        ? { tone: 'bad' as const, label: 'REGRESSED since' }
        : { tone: 'warn' as const, label: 'not re-measured since' }
  return (
    <li className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm">
      <span className="font-mono">{fix.case_id}</span>
      <Badge tone={state.tone}>{state.label}</Badge>
      <span className="text-xs text-muted">proven {when(fix.at)}</span>
      {fix.last_seen_at && (
        <span className="ml-auto text-xs text-muted">last scored {when(fix.last_seen_at)}</span>
      )}
    </li>
  )
}

/**
 * Recall over time, with a break wherever the measurement changed.
 *
 * Drawn as separate polylines rather than one line with markers: a continuous stroke across a judge
 * change is a picture of a trend that does not exist, and no amount of legend undoes what the eye
 * already did.
 */
function TrendChart({ points }: { points: TrendPoint[] }) {
  const width = 640
  const height = 120
  const pad = 8
  const step = points.length > 1 ? (width - pad * 2) / (points.length - 1) : 0
  const x = (i: number) => pad + i * step
  const y = (v: number) => pad + (1 - v) * (height - pad * 2)

  // Split into runs of comparable points; each becomes its own stroke.
  const segments: { i: number; point: TrendPoint }[][] = []
  points.forEach((point, i) => {
    if (i === 0 || !point.comparable) segments.push([])
    segments[segments.length - 1]!.push({ i, point })
  })

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="w-full rounded-lg border border-line bg-surface"
      role="img"
      aria-label={`recall across ${points.length} runs, in ${segments.length} comparable segment(s)`}
    >
      <line
        x1={pad}
        y1={y(1)}
        x2={width - pad}
        y2={y(1)}
        className="stroke-line"
        strokeDasharray="3 3"
      />
      {segments.map((segment, s) => (
        <g key={s}>
          {segment.length > 1 && (
            <polyline
              points={segment.map(({ i, point }) => `${x(i)},${y(point.recall)}`).join(' ')}
              fill="none"
              className="stroke-accent"
              strokeWidth="2"
              strokeLinejoin="round"
            />
          )}
          {segment.map(({ i, point }) => (
            <circle
              key={point.run_id}
              cx={x(i)}
              cy={y(point.recall)}
              r="3"
              className="fill-accent"
            >
              <title>{`${when(point.created_at)} — recall ${point.recall.toFixed(3)} over ${point.cases} case(s)`}</title>
            </circle>
          ))}
        </g>
      ))}
      {/* A dashed rule at every seam, so the break is visible and not merely implied by the gap. */}
      {points.map((point, i) =>
        i > 0 && !point.comparable ? (
          <line
            key={point.run_id}
            x1={x(i) - step / 2}
            y1={pad}
            x2={x(i) - step / 2}
            y2={height - pad}
            className="stroke-warn"
            strokeDasharray="2 3"
          />
        ) : null,
      )}
    </svg>
  )
}

function PointRow({ point }: { point: TrendPoint }) {
  const added = point.cases_added ?? []
  const seams = [
    point.judge_changed && 'judge changed',
    point.reviewer_changed && 'model changed',
    point.corpus_changed && `corpus changed${added.length ? ` (+${added.length})` : ''}`,
  ].filter(Boolean) as string[]

  return (
    <li className="flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm">
      <span className="text-muted">{when(point.created_at)}</span>
      <span className="tabular">recall {score(point.recall, 2)}</span>
      <span className="tabular">fp {score(point.fp_rate, 2)}</span>
      {point.holdout_recall !== null && (
        <span className="tabular text-muted" title="Holdout: cases the improve loop never sees">
          holdout {score(point.holdout_recall, 2)}
        </span>
      )}
      <span className="text-xs text-muted">{point.cases} case(s)</span>
      {point.guidance_changed && (
        <Badge tone="accent" title="The guidance differs from the run before this one">
          guidance edited
        </Badge>
      )}
      {seams.map((seam) => (
        <Badge key={seam} tone="warn" title="Not comparable with the run before it">
          {seam}
        </Badge>
      ))}
    </li>
  )
}
