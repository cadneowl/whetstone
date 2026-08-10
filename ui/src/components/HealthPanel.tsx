import { Link } from 'react-router-dom'
import {
  useHealth,
  useMarkDistill,
  useSetTier,
  type CadenceClock,
  type Retirement,
  type SkillHealth,
  type UncoveredMr,
} from '@/api/client'
import { FitPanel } from '@/components/FitPanel'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Loading, score, when } from '@/components/primitives'
import { mergeRequestOf } from '@/routes/triageFilters'

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
      <CompositionSection skillId={skillId} health={data} />
      <RetirementSection skillId={skillId} retirements={data.retirements ?? []} />
      <DiscriminationSection skillId={skillId} health={data} />
      <DriftSection skillId={skillId} health={data} />
      <IndexSection skillId={skillId} health={data} />
      <JudgeSection health={data} />
      <ProductionSection health={data} />
      <CadenceSection skillId={skillId} health={data} />
      {/* Beside the dead-rule report rather than among the score sections: both are about the
          guidance as written rather than about the corpus, and this one is the question the score
          sections cannot raise — whether the rules being measured actually reach the model. */}
      <Section
        title="Context fit"
        intro="Whether the guidance fits the window it is served through, and what every review pays before the diff arrives. Arithmetic, not a score."
      >
        <FitPanel skillId={skillId} />
      </Section>
      <DeadRulesSection skillId={skillId} health={data} />
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
            train {score(h.train_recall, 2)}{' '}
            <span className="text-xs text-muted">({h.train_cases})</span>
          </span>
          <span className="tabular">
            holdout {score(h.holdout_recall, 2)}{' '}
            <span className="text-xs text-muted">({h.holdout_cases})</span>
          </span>
          <span className="tabular text-muted">divergence {score(h.divergence, 2)}</span>
          {h.diverging && (
            <Badge tone="warn" title={h.reading}>
              overfitting?
            </Badge>
          )}
        </div>
      ) : (
        <p className="mt-2 border-t border-line pt-2 text-xs text-muted italic">
          No holdout cases scored in this run.
        </p>
      )}
      {/* The reading, not a number: on this panel the useful thing is whether the alarm can fire
          at all. A gap the holdout is too small to resolve reads here as what it is, along with
          how many more graduated cases would make it readable. */}
      {h && h.holdout_cases > 0 && <p className="mt-2 text-xs text-muted">{h.reading}.</p>}
    </Section>
  )
}

function CompositionSection({ skillId, health }: { skillId: string; health: SkillHealth }) {
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
        {(c.synthetic ?? 0) > 0 && (
          <span
            className="tabular text-muted"
            title="Generated from parent cases, not mined from review history — their authority is inherited, and every corpus statistic can tell them apart"
          >
            {c.synthetic} synthetic
          </span>
        )}
        {c.noflag > 0 && (
          <span className="text-xs text-muted">
            precision evidence: {confirmed} confirmed · {silence} from silence
            {(mix.synthetic ?? 0) > 0 && ` · ${mix.synthetic} synthetic`}
            {(mix.unclassified ?? 0) > 0 && ` · ${mix.unclassified} hand-written`}
          </span>
        )}
        {silence > confirmed && c.noflag > 0 && (
          <Badge tone="warn" title="fp_rate is mostly measuring that nobody commented">
            precision rests on silence
          </Badge>
        )}
      </div>
      {/* The generators that grow this corpus without waiting for the next incident. Both write
          to triage — a person still rules on every candidate before it counts. */}
      <div className="mt-3 flex flex-wrap items-start gap-3 border-t border-line pt-3">
        <LaunchButton
          kind="synthesize"
          request={{ skill_id: skillId, mode: 'counterfactual' }}
          label="Generate counterfactuals"
        />
        <LaunchButton
          kind="synthesize"
          request={{ skill_id: skillId, mode: 'mutation' }}
          label="Draft mutation probes"
        />
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
        {retirements.map((r) => {
          // Only the row being archived shows the pending state — one shared mutation drove every
          // button to "Archiving…" and disabled the whole list on a single click.
          const pending = flip.isPending && flip.variables?.caseId === r.case_id
          return (
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
                disabled={pending}
                onClick={() => flip.mutate({ caseId: r.case_id, tier: 'archive' })}
                className="ml-auto rounded border border-line px-2 py-0.5 text-xs transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted"
              >
                {pending ? 'Archiving…' : 'Archive'}
              </button>
            </li>
          )
        })}
      </ul>
      {flip.error && <ErrorNote error={flip.error} />}
    </Section>
  )
}

/**
 * The saturation probe's readout: which active should-catch cases the naked model already passes.
 *
 * A flagged case never measured the guidance — either the base model knows the lesson (retire
 * it) or the expectation is loose enough that anything matches (tighten it). Both are the
 * operator's call; the probe only produces the evidence.
 */
function DiscriminationSection({ skillId, health }: { skillId: string; health: SkillHealth }) {
  const flip = useSetTier(skillId)
  const d = health.discrimination
  return (
    <Section
      title="Discrimination"
      intro="Cases scored with the guidance stripped. A should-catch case the naked model passes measures nothing — the score credits the guidance for what the base model already knew."
    >
      {d ? (
        <div className="space-y-2">
          <p className="tabular">
            {d.testing_guidance} of {d.active_catch} active catch case
            {d.active_catch === 1 ? '' : 's'} still measure the guidance
            <span className="ml-2 text-xs text-muted">probed {when(d.measured_at)}</span>
          </p>
          {(d.flagged ?? []).length > 0 && (
            <ul className="space-y-1.5 border-t border-line pt-2">
              {(d.flagged ?? []).map((c) => {
                const pending = flip.isPending && flip.variables?.caseId === c.case_id
                return (
                  <li key={c.case_id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <Link
                      to={`/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(c.case_id)}`}
                      className="font-mono text-xs hover:text-accent"
                    >
                      {c.case_id}
                    </Link>
                    <span className="text-xs text-muted">
                      passes with no guidance — tighten its expectation or retire it
                    </span>
                    <button
                      type="button"
                      disabled={pending}
                      onClick={() => flip.mutate({ caseId: c.case_id, tier: 'archive' })}
                      className="ml-auto rounded border border-line px-2 py-0.5 text-xs transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted"
                    >
                      {pending ? 'Archiving…' : 'Archive'}
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
          {flip.error && <ErrorNote error={flip.error} />}
        </div>
      ) : (
        <p className="text-muted italic">
          Never probed — the baseline scores every active case with an empty skill body.
        </p>
      )}
      <div className="mt-3">
        <LaunchButton
          kind="baseline"
          request={{ skill_id: skillId }}
          label={d ? 'Re-run baseline probe' : 'Run baseline probe'}
        />
      </div>
    </Section>
  )
}

/**
 * Whether the corpus still looks like what the team actually ships.
 *
 * Coverage is the number that matters: the fraction of recent merge requests with an active case
 * nearby. The uncovered list is the remedy — each row is an MR that looks like nothing the skill
 * is tested on, linked into triage where promoting a candidate from it is one click away.
 */
function DriftSection({ skillId, health }: { skillId: string; health: SkillHealth }) {
  const d = health.drift
  return (
    <Section
      title="Drift"
      intro="Recent merge requests, embedded and compared against the active cases. An MR with no case within the similarity radius is one the scores above say nothing about."
    >
      {d ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
            <span className="tabular">
              coverage {score(d.report.coverage, 2)}
              <span className="ml-1 text-xs text-muted">
                of {d.report.recent_mrs} recent MR{d.report.recent_mrs === 1 ? '' : 's'}
              </span>
            </span>
            <span
              className="tabular text-muted"
              title="1 − cosine similarity of the two centroids — growth means the middle of the stream is moving away from the middle of the corpus"
            >
              centroid distance {score(d.report.centroid_distance, 3)}
            </span>
            {d.alarm && (
              <Badge
                tone="warn"
                title="Past the drift threshold — the eval may be scoring last year's idioms"
              >
                {Math.round((d.report.uncovered_fraction ?? 0) * 100)}% uncovered
              </Badge>
            )}
            <span className="ml-auto text-xs text-muted">probed {when(d.report.measured_at)}</span>
          </div>
          {(d.history ?? []).length > 0 && (
            <p className="text-xs text-muted">
              trend:{' '}
              {[...(d.history ?? [])]
                .reverse()
                .map((p) => score(p.coverage, 2))
                .concat([`${score(d.report.coverage, 2)} now`])
                .join(' → ')}
            </p>
          )}
          {(d.report.uncovered ?? []).length > 0 && (
            <ul className="space-y-1.5 border-t border-line pt-2">
              {(d.report.uncovered ?? []).map((mr) => (
                <UncoveredRow key={mr.ref} mr={mr} />
              ))}
            </ul>
          )}
          {(d.report.uncovered_total ?? 0) > (d.report.uncovered ?? []).length && (
            <p className="text-xs text-muted italic">
              … and {(d.report.uncovered_total ?? 0) - (d.report.uncovered ?? []).length} more
              uncovered — the report keeps the farthest {(d.report.uncovered ?? []).length}.
            </p>
          )}
        </div>
      ) : (
        <p className="text-muted italic">
          Never probed — the drift probe embeds the corpus and the candidate queue through a local
          model (set <code className="font-mono">[drift] embed_model</code> in whetstone.toml).
        </p>
      )}
      <div className="mt-3">
        <LaunchButton
          kind="drift"
          request={{ skill_id: skillId }}
          label={d ? 'Re-run drift probe' : 'Run drift probe'}
        />
      </div>
    </Section>
  )
}

/**
 * One uncovered merge request, linked into triage scoped to *all* of its candidates.
 *
 * The row used to open one candidate. But the promotion this list is arguing for is rarely one
 * case — an MR nothing in the corpus resembles usually has several threads worth mining, and the
 * queue is sorted by confidence, so its siblings are scattered through it. Scoping the queue to
 * the merge request puts them together and still lands on the one the report picked.
 */
function UncoveredRow({ mr }: { mr: UncoveredMr }) {
  const scoped =
    `/triage?mr=${encodeURIComponent(mergeRequestOf(mr.ref))}` +
    `&focus=${encodeURIComponent(mr.candidate_id)}`
  return (
    <li className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      {mr.pending ? (
        <Link
          to={scoped}
          title="Opens triage with the queue narrowed to this merge request"
          className="font-mono text-xs hover:text-accent"
        >
          {mr.ref}
        </Link>
      ) : (
        <span className="font-mono text-xs" title="its candidates have already been ruled on">
          {mr.ref}
        </span>
      )}
      {mr.title && <span className="text-xs text-muted">{mr.title}</span>}
      <span className="ml-auto text-xs text-muted tabular">
        {mr.nearest_case
          ? `nearest ${mr.nearest_case} at ${(mr.similarity ?? 0).toFixed(2)}`
          : 'no case comes close'}
      </span>
    </li>
  )
}

/**
 * The committed retrieval index — what precedent injection reads at review time.
 *
 * A rebuild is a content change: the index folds into skill_hash, so the job stages it on the
 * skill's branch and the gate must re-prove the skill before it can be proposed. Staleness is
 * not an error, it is the newest lessons not yet retrievable.
 */
function IndexSection({ skillId, health }: { skillId: string; health: SkillHealth }) {
  const idx = health.index
  return (
    <Section
      title="Case index"
      intro="Precedents at review time: the incoming change is embedded with the pinned model and the nearest cases are injected as calibration — a case promoted this morning sharpens this afternoon's reviews, no improve cycle needed."
    >
      {idx ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
            <span className="tabular">
              {idx.cases} case{idx.cases === 1 ? '' : 's'} indexed
            </span>
            <span className="font-mono text-xs text-muted">
              {idx.model}
              {idx.provider ? ` via ${idx.provider}` : ''}
            </span>
            {idx.built_at && <span className="text-xs text-muted">built {idx.built_at}</span>}
            {(idx.stale ?? []).length > 0 && (
              <Badge
                tone="warn"
                title="Active cases the index does not cover — promoted or edited since the last build. Rebuild to make them retrievable."
              >
                {(idx.stale ?? []).length} not indexed
              </Badge>
            )}
          </div>
          {(idx.stale ?? []).length > 0 && (
            <p className="text-xs text-muted">
              not yet retrievable:{' '}
              {(idx.stale ?? []).map((caseId, i) => (
                <span key={caseId}>
                  {i > 0 && ', '}
                  <Link
                    to={`/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(caseId)}`}
                    className="font-mono hover:text-accent"
                  >
                    {caseId}
                  </Link>
                </span>
              ))}
            </p>
          )}
        </div>
      ) : (
        <p className="text-muted italic">
          No index — the reviewer sees guidance and wiki only, exactly as before the feature
          existed. Building one pins an embedding model and stages the vectors on the skill's
          branch.
        </p>
      )}
      <div className="mt-3">
        <LaunchButton
          kind="index"
          request={{ skill_id: skillId }}
          label={idx ? 'Rebuild case index' : 'Build case index'}
        />
      </div>
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

const CLOCK_NAMES: Record<CadenceClock['kind'], string> = {
  distill: 'Guidance distill pass',
  saturation: 'Saturation probe',
  anchor: 'Full-corpus anchor run',
  drift: 'Drift probe',
}

/**
 * The routine clocks — the upkeep nothing ever fails loudly enough to demand.
 *
 * Three clocks reset themselves when their pass runs (the probes record themselves; the anchor is
 * read from the run store). Only the distill pass is hand-marked, because a distill is an ordinary
 * improve run with a consolidating instruction and nothing in its record distinguishes it.
 */
function CadenceSection({ skillId, health }: { skillId: string; health: SkillHealth }) {
  const mark = useMarkDistill(skillId)
  const clocks = health.cadence?.clocks ?? []
  return (
    <Section
      title="Cadence"
      intro="Routine passes on a clock: monthly distill and saturation probe, quarterly anchor run and drift review. A clock that has never fired starts counting from the skill's first run."
    >
      <ul className="space-y-1.5">
        {clocks.map((c) => (
          <li key={c.kind} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="w-44">{CLOCK_NAMES[c.kind]}</span>
            <span className="text-xs text-muted tabular">every {c.period_days}d</span>
            <span className="text-xs text-muted">
              {c.last_done ? `last done ${when(c.last_done)}` : 'never done'}
            </span>
            {c.due && (
              <Badge tone="warn" title={c.label || undefined}>
                due
              </Badge>
            )}
            {c.kind === 'distill' && (
              <button
                type="button"
                disabled={mark.isPending}
                onClick={() => mark.mutate()}
                title="Record that a distill pass ran — an improve with a consolidating instruction, gated over the full corpus including the archive"
                className="ml-auto rounded border border-line px-2 py-0.5 text-xs transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted"
              >
                {mark.isPending ? 'Marking…' : 'Mark done'}
              </button>
            )}
          </li>
        ))}
      </ul>
      {mark.error && <ErrorNote error={mark.error} />}
    </Section>
  )
}

/**
 * Rules the evidence no longer stands behind — the removal list the distill pass reads before it
 * deletes anything. An empty report renders nothing: no rule provenance, or all of it healthy.
 */
function DeadRulesSection({ skillId, health }: { skillId: string; health: SkillHealth }) {
  const rules = health.dead_rules ?? []
  if (rules.length === 0) return null
  return (
    <Section
      title={`Dead rules (${rules.length})`}
      intro="meta.yaml rule provenance crossed with the corpus. A rule listed here could vanish without any case going red — evidence for the distill pass, never an automatic deletion."
    >
      <ul className="space-y-1.5">
        {rules.map((r) => (
          <li key={r.rule_id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-mono text-xs">{r.rule_id}</span>
            <Badge tone="warn">{r.verdict}</Badge>
            <span className="text-xs text-muted">{r.evidence}</span>
            {(r.case_ids ?? []).length > 0 && (
              <span className="text-xs text-muted">
                {(r.case_ids ?? []).map((caseId, i) => (
                  <span key={caseId}>
                    {i > 0 && ', '}
                    <Link
                      to={`/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(caseId)}`}
                      className="font-mono hover:text-accent"
                    >
                      {caseId}
                    </Link>
                  </span>
                ))}
              </span>
            )}
          </li>
        ))}
      </ul>
    </Section>
  )
}
