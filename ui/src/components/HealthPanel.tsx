import { Link } from 'react-router-dom'
import { useHealth, useSetTier, type Retirement, type SkillHealth } from '@/api/client'
import { Badge, Empty, ErrorNote, Loading, score, when } from '@/components/primitives'

/**
 * One skill's state of affairs on one surface — the integrating panel of the anti-rot plan.
 *
 * Every mechanism below reports somewhere else too (runs carry the holdout pair, the judge has its
 * own page, rulings live on reviews); this is where they sit in one eyeline, because "how is this
 * skill actually doing?" is one question. Sections whose measurements have not been built yet say
 * so explicitly — a health surface that hides what it cannot see reads as healthier than it is.
 */
export function HealthPanel({ skillId }: { skillId: string }) {
  const { data, isLoading, error } = useHealth(skillId)

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>No health data.</Empty>

  return (
    <div className="max-w-3xl space-y-5">
      <ScoreSection health={data} />
      <CompositionSection health={data} />
      <RetirementSection skillId={skillId} retirements={data.retirements ?? []} />
      <JudgeSection health={data} />
      <ProductionSection health={data} />
      <PendingSections />
    </div>
  )
}

function Section({
  title,
  intro,
  children,
}: {
  title: string
  intro?: string
  children: React.ReactNode
}) {
  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      <h3 className="text-sm font-medium">{title}</h3>
      {intro && <p className="mt-0.5 mb-2 text-xs text-muted">{intro}</p>}
      <div className="mt-2 text-sm">{children}</div>
    </section>
  )
}

/** Matches the drill-down's alarm: a run whose train side leads holdout by this much is suspect. */
const DIVERGENCE_ALARM = 0.1

function ScoreSection({ health }: { health: SkillHealth }) {
  const s = health.score
  if (!s) {
    return (
      <Section title="Score">
        <span className="text-muted italic">Never evaluated — run evals from the header.</span>
      </Section>
    )
  }
  const h = s.holdout
  return (
    <Section
      title="Score"
      intro="The latest run. Train is what the improve loop learns from; holdout is the slice it never sees — a widening gap is memorization, not improvement."
    >
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
        <span className="tabular">recall {score(s.recall, 2)}</span>
        <span className="tabular">fp {score(s.fp_rate, 2)}</span>
        <span className="tabular">F2 {score(s.f2, 2)}</span>
        <Link
          to={`/runs/${encodeURIComponent(s.run_id)}`}
          className="ml-auto text-xs text-muted underline hover:text-accent"
        >
          {when(s.created_at)} →
        </Link>
      </div>
      {h ? (
        <div className="mt-2 flex flex-wrap items-baseline gap-x-5 gap-y-1 border-t border-line pt-2">
          <span className="tabular">
            train {score(h.train_recall, 2)} <span className="text-xs text-muted">({h.train_cases})</span>
          </span>
          <span className="tabular">
            holdout {score(h.holdout_recall, 2)}{' '}
            <span className="text-xs text-muted">({h.holdout_cases})</span>
          </span>
          <span className="tabular text-muted">divergence {score(h.divergence, 2)}</span>
          {h.divergence > DIVERGENCE_ALARM && (
            <Badge tone="warn" title="Train runs well ahead of holdout — the guidance may be memorizing its exam">
              overfitting?
            </Badge>
          )}
        </div>
      ) : (
        <p className="mt-2 border-t border-line pt-2 text-xs text-muted italic">
          No holdout cases scored in this run.
        </p>
      )}
    </Section>
  )
}

function CompositionSection({ health }: { health: SkillHealth }) {
  const c = health.composition
  const mix = c.evidence_mix ?? {}
  const silence = mix.silence ?? 0
  const confirmed = mix.confirmed ?? 0
  return (
    <Section
      title="Corpus"
      intro="What the scores are computed over. Archive cases are lessons already internalized — sampled at low weight as regression insurance."
    >
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
        <span className="tabular">{c.active} active</span>
        <span className="tabular text-muted">{c.archive} archived</span>
        <span className="tabular text-muted">
          {c.catch} catch / {c.noflag} noflag
        </span>
        {c.noflag > 0 && (
          <span className="text-xs text-muted">
            precision evidence: {confirmed} confirmed · {silence} from silence
            {(mix.unclassified ?? 0) > 0 && ` · ${mix.unclassified} hand-written`}
          </span>
        )}
        {silence > confirmed && c.noflag > 0 && (
          <Badge tone="warn" title="fp_rate is mostly measuring that nobody commented">
            precision rests on silence
          </Badge>
        )}
      </div>
    </Section>
  )
}

function RetirementSection({
  skillId,
  retirements,
}: {
  skillId: string
  retirements: Retirement[]
}) {
  const flip = useSetTier(skillId)
  if (retirements.length === 0) return null
  return (
    <Section
      title={`Ready to retire (${retirements.length})`}
      intro="Cases every recent gate passed on every version — they no longer tell a better rule from a worse one. Archiving stages a one-line commit on the skill branch; C6 asks for a fresh gate before it ships. Reversible the same way."
    >
      <ul className="space-y-1.5">
        {retirements.map((r) => (
          <li key={r.case_id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <Link
              to={`/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(r.case_id)}`}
              className="font-mono text-xs hover:text-accent"
            >
              {r.case_id}
            </Link>
            <span className="text-xs text-muted">{r.evidence}</span>
            <button
              type="button"
              disabled={flip.isPending}
              onClick={() => flip.mutate({ caseId: r.case_id, tier: 'archive' })}
              className="ml-auto rounded border border-line px-2 py-0.5 text-xs transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted"
            >
              {flip.isPending ? 'Archiving…' : 'Archive'}
            </button>
          </li>
        ))}
      </ul>
      {flip.error && <ErrorNote error={flip.error} />}
    </Section>
  )
}

function JudgeSection({ health }: { health: SkillHealth }) {
  const j = health.judge
  if (health.judge_error) {
    return (
      <Section title="Judge">
        <p className="text-sm text-bad">{health.judge_error}</p>
      </Section>
    )
  }
  if (!j) return null
  return (
    <Section
      title="Judge"
      intro="The instrument every number above came through — one judge serves the whole deployment."
    >
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span>
          {j.id} v{j.version}
        </span>
        {j.builtin && <Badge tone="neutral">built-in</Badge>}
        {j.measured ? (
          <span className="tabular">
            accuracy {score(j.measured.accuracy, 2)}{' '}
            <span className="text-xs text-muted">
              vs bar {score(j.bar, 2)} · {j.measured.missed} missed / {j.measured.spurious} spurious
            </span>
          </span>
        ) : (
          <span className="text-xs text-muted italic">
            this doctrine has not been measured — {j.pairs_total} labeled pair
            {j.pairs_total === 1 ? '' : 's'} waiting
          </span>
        )}
        <Link to="/judge" className="ml-auto text-xs text-muted underline hover:text-accent">
          judge page →
        </Link>
      </div>
    </Section>
  )
}

function ProductionSection({ health }: { health: SkillHealth }) {
  const p = health.production
  return (
    <Section
      title="Production"
      intro="Human rulings on live findings — the ground truth the eval scores above are a proxy for. When these two disagree, believe this one."
    >
      {p ? (
        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
          <span className="tabular">{p.reviews} reviews</span>
          <span className="tabular text-good">{p.confirmed} confirmed</span>
          <span className="tabular text-bad">{p.rejected} rejected</span>
          {p.pending > 0 && <span className="tabular text-muted">{p.pending} unruled</span>}
        </div>
      ) : (
        <span className="text-muted italic">No live reviews recorded for this skill yet.</span>
      )}
    </Section>
  )
}

/**
 * Measurements the plan defines that have not been built yet, named rather than omitted. A blank
 * where a number should be is the honest state; a panel that only shows what exists would read as
 * complete when it is not.
 */
function PendingSections() {
  const pending = [
    ['Discrimination', 'which cases still measure the guidance — lands with the zero-guidance baseline probe'],
    ['Drift', 'whether the corpus still looks like the MR stream — lands with the drift metric'],
    ['Cadence', 'when the maintenance passes last ran and what is due — lands with the cadence clocks'],
  ] as const
  return (
    <section className="rounded-lg border border-dashed border-line p-4 text-xs text-muted">
      <h3 className="mb-1 text-sm font-medium text-muted">Not yet measured</h3>
      <ul className="space-y-0.5">
        {pending.map(([name, what]) => (
          <li key={name}>
            <span className="font-medium">{name}</span> — {what}
          </li>
        ))}
      </ul>
    </section>
  )
}
