import { Link, useParams } from 'react-router-dom'
import { useCase } from '@/api/client'
import { DiffView, type Overlay } from '@/components/diff/DiffView'
import {
  Badge,
  Empty,
  ErrorNote,
  Intro,
  Loading,
  score,
  severityName,
  when,
} from '@/components/primitives'

export function CaseDetail() {
  const { skillId = '', caseId = '' } = useParams()
  const { data, isLoading, error } = useCase(skillId, caseId)

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  const { case: evalCase, diff, history, baseline } = data
  const isCatch = evalCase.kind === 'should_catch'

  const overlays: Overlay[] = evalCase.expect.map((e) => ({
    // No line_range means "anywhere in the file", which is a whole-file highlight.
    range: (e.where.line_range ?? [1, Number.MAX_SAFE_INTEGER]) as [number, number],
    wholeFile: !e.where.line_range,
    path: e.where.path,
    kind: 'expectation',
    tone: isCatch ? 'accent' : 'warn',
    label: e.semantic || e.id,
  }))

  return (
    <div>
      <nav className="mb-3 text-sm text-muted">
        <Link to={`/skills/${encodeURIComponent(skillId)}`} className="hover:text-ink">
          ← {skillId}
        </Link>
      </nav>

      <header className="mb-4">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="font-mono text-lg font-semibold">{evalCase.id}</h1>
          <Badge tone={isCatch ? 'accent' : 'neutral'}>
            {isCatch ? 'should catch' : 'should not flag'}
          </Badge>
          {evalCase.provenance.ref && (
            <span className="font-mono text-xs text-muted">{evalCase.provenance.ref}</span>
          )}
          {evalCase.provenance.human_signal && (
            <span className="text-xs text-muted">“{evalCase.provenance.human_signal}”</span>
          )}
          {evalCase.tier === 'archive' && (
            <Badge tone="neutral" title="Retired: drawn at low weight as regression insurance">
              archived
            </Badge>
          )}
          {/* The saturation probe's verdict. For a catch case, "passed with no guidance" means
              the case measures nothing — the base model already knows the lesson, or the
              expectation is loose enough that anything matches. */}
          {baseline && isCatch && baseline.passed && (
            <Badge tone="warn" title={`Probed ${when(baseline.created_at)}`}>
              passes with no guidance
            </Badge>
          )}
          {baseline && isCatch && !baseline.passed && (
            <span className="text-xs text-muted" title={`Probed ${when(baseline.created_at)}`}>
              naked model misses this — the case measures the guidance
            </span>
          )}
        </div>
        <Intro>
          One real review outcome, frozen as a test. The badge is what a human decided; the quoted
          signal beside it is what they actually did on that merge request. The expectation on the
          right is the ground truth every finding is judged against — and History is whether this
          skill has been getting it right.
        </Intro>
      </header>

      <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
        <div>
          <DiffView diff={diff} overlays={overlays} />
        </div>

        <aside className="space-y-5">
          <section>
            <h2 className="mb-2 text-xs tracking-wide text-muted uppercase">Expectations</h2>
            <ul className="space-y-2">
              {evalCase.expect.map((e) => (
                <li key={e.id} className="rounded-lg border border-line bg-surface px-3 py-2">
                  <p className="text-sm">
                    must <strong className="font-semibold">{e.must.replace('_', ' ')}</strong>
                  </p>
                  {e.semantic && <p className="mt-1 text-sm text-muted">{e.semantic}</p>}
                  <p className="mt-1 font-mono text-xs text-muted">
                    {e.where.path}
                    {e.where.line_range && ` : ${e.where.line_range[0]}–${e.where.line_range[1]}`}
                  </p>
                  {e.severity_min !== null && e.severity_min !== undefined && (
                    <p className="mt-1 text-xs text-muted">
                      severity ≥ {severityName(e.severity_min)}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h2 className="mb-2 text-xs tracking-wide text-muted uppercase">History</h2>
            {history.length === 0 ? (
              <p className="text-sm text-muted italic">Never evaluated.</p>
            ) : (
              <ul className="space-y-1">
                {history.map((h) => (
                  <li key={h.run_id}>
                    <Link
                      to={`/runs/${encodeURIComponent(h.run_id)}`}
                      className="flex items-baseline gap-2 rounded px-1 py-0.5 text-sm hover:bg-surface"
                    >
                      <span className="text-xs text-muted">{when(h.created_at)}</span>
                      <span className="ml-auto tabular">
                        {isCatch ? score(h.recall, 2) : score(h.fp_rate, 2)}
                      </span>
                      {h.flaky && <span title="trials disagreed">⚠</span>}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
      </div>
    </div>
  )
}
