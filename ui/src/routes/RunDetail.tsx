import { Link, useParams } from 'react-router-dom'
import type {
  CaseRun,
  Dispute,
  DisputeRequest,
  ExpectationOutcome,
  Finding,
  RunRecord,
  TrialRecord,
} from '@/api/client'
import { useConsoleConfig, useDisputes, useDisputeVerdict, useRun } from '@/api/client'
import {
  Badge,
  Empty,
  ErrorNote,
  Intro,
  Loading,
  Metric,
  OUTCOME_TITLE,
  OutcomeChip,
  score,
  severityName,
  when,
} from '@/components/primitives'

/**
 * "Why did this fail?"
 *
 * A flaky case shows up elsewhere as `recall 0.60` and nothing more, which is indistinguishable
 * between three different problems: the reviewer missed the issue, the judge ruled wrongly, or the
 * expectation is worded badly. They have three different fixes, so this screen shows the evidence
 * for each one — every finding, and the judge's reason for accepting or rejecting it.
 */
export function RunDetail() {
  const { runId = '' } = useParams()
  const { data, isLoading, error } = useRun(runId)
  const { data: config } = useConsoleConfig()
  const { data: disputes } = useDisputes(runId)

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  // One ruling per verdict address; the newest wins, which the backend guarantees by replacement.
  const rulings = new Map<string, Dispute>()
  for (const d of disputes ?? []) {
    const key = rulingKey(d.case_id, d.trial, d.expectation_id, d.finding_index)
    if (!rulings.has(key)) rulings.set(key, d)
  }

  return (
    <div>
      <nav className="mb-3 text-sm text-muted">
        <Link to={`/skills/${encodeURIComponent(data.skill_id)}`} className="hover:text-ink">
          ← {data.skill_id}
        </Link>
      </nav>

      <Header run={data} />

      <h2 className="mt-6 mb-2 text-xs tracking-wide text-muted uppercase">Cases</h2>
      {data.cases.length === 0 ? (
        <Empty>This run covered no eval cases.</Empty>
      ) : (
        <div className="space-y-2">
          {data.cases.map((c) => (
            <CaseBlock
              key={c.case_id}
              run={data}
              caseRun={c}
              rulings={rulings}
              readOnly={config?.read_only ?? true}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function rulingKey(caseId: string, trial: number, expectationId: string, findingIndex: number) {
  return `${caseId}|${trial}|${expectationId}|${findingIndex}`
}

function Header({ run }: { run: RunRecord }) {
  const s = run.score
  return (
    <header>
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-lg font-semibold">{run.skill_id}</h1>
        <span className="font-mono text-sm text-muted">v{run.skill_version}</span>
        {run.practice_mode && (
          <Badge tone="warn" title="Deterministic doubles — no model was called">
            practice mode
          </Badge>
        )}
        <a
          href={`/api/runs/${encodeURIComponent(run.id)}/report`}
          target="_blank"
          rel="noreferrer"
          className="ml-auto text-sm text-accent hover:underline"
        >
          standalone report ↗
        </a>
      </div>

      <p className="mt-1 font-mono text-xs text-muted">{run.id}</p>

      <Intro>
        One scoring pass, with the whole chain behind every number. Expand a case to see what the
        reviewer reported, which findings the region and severity filters even let through, and the
        judge's reason for accepting or rejecting each one — because a failing case means either the
        guidance is wrong or the eval case is, and this is how you tell which.
      </Intro>

      <div className="mt-3 flex flex-wrap gap-2">
        <Metric label="recall" value={score(s.recall)} />
        <Metric label="fp rate" value={score(s.fp_rate)} />
        <Metric label="precision" value={score(s.precision)} />
        {run.k > 1 && <Metric label="recall stdev" value={score(s.recall_stdev)} />}
      </div>

      <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
        <li>{when(run.created_at)}</li>
        <li>
          backend <code className="font-mono">{run.backend || '—'}</code>
        </li>
        <li>
          model <code className="font-mono">{run.model || '—'}</code>
        </li>
        <li>k={run.k}</li>
        <li>
          effort {run.reviewer_effort}/{run.judge_effort}
        </li>
        <li>{run.llm_calls} llm calls</li>
        <li>{run.duration_s.toFixed(1)}s</li>
        <li title="Content identity — comparison keys on this, not the hand-edited version number">
          hash <code className="font-mono">{run.skill_hash.slice(0, 12)}</code>
        </li>
        <li title="Identity of the judge whose verdicts this run is built from. Scores from different judges are different measurements — compare runs only within one judge.">
          judge{' '}
          <code className="font-mono">
            {run.judge_hash ? run.judge_hash.slice(0, 12) : 'pre-attribution'}
          </code>
        </li>
        {run.principal && <li>by {run.principal}</li>}
      </ul>
    </header>
  )
}

function CaseBlock({
  run,
  caseRun,
  rulings,
  readOnly,
}: {
  run: RunRecord
  caseRun: CaseRun
  rulings: Map<string, Dispute>
  readOnly: boolean
}) {
  const isCatch = caseRun.kind === 'should_catch'
  const totals = caseRun.trials.flatMap((t) => t.outcomes)
  const good = totals.filter((o) => o.outcome === 'tp' || o.outcome === 'tn').length
  const flaky = isFlaky(caseRun)

  return (
    <details className="rounded-lg border border-line bg-surface">
      <summary className="flex cursor-pointer flex-wrap items-center gap-2 px-3 py-2.5 text-sm">
        <Badge tone={isCatch ? 'accent' : 'neutral'}>
          {isCatch ? 'should catch' : 'should not flag'}
        </Badge>
        <Link
          to={`/skills/${encodeURIComponent(run.skill_id)}/cases/${encodeURIComponent(caseRun.case_id)}`}
          className="font-mono font-medium hover:text-accent"
          onClick={(e) => e.stopPropagation()}
        >
          {caseRun.case_id}
        </Link>
        {flaky && (
          <Badge tone="warn" title="Trials disagreed — unstable, as opposed to simply wrong">
            flaky
          </Badge>
        )}
        <span className="ml-auto flex items-center gap-1.5">
          {totals.map((o, i) => (
            <OutcomeChip key={i} outcome={o.outcome} />
          ))}
          <span className="ml-2 tabular text-muted">
            {good}/{totals.length}
          </span>
        </span>
      </summary>

      <div className="space-y-2 px-3 pt-1 pb-3 pl-6">
        {caseRun.trials.map((trial) => (
          <TrialBlock
            key={trial.index}
            runId={run.id}
            caseId={caseRun.case_id}
            trial={trial}
            total={caseRun.trials.length}
            rulings={rulings}
            readOnly={readOnly}
          />
        ))}
      </div>
    </details>
  )
}

function TrialBlock({
  runId,
  caseId,
  trial,
  total,
  rulings,
  readOnly,
}: {
  runId: string
  caseId: string
  trial: TrialRecord
  total: number
  rulings: Map<string, Dispute>
  readOnly: boolean
}) {
  return (
    <details className="rounded-lg border border-line bg-canvas" open={total === 1}>
      <summary className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm">
        <strong className="font-medium">
          Trial {trial.index + 1} of {total}
        </strong>
        <span className="ml-auto flex gap-1.5">
          {trial.outcomes.map((o, i) => (
            <OutcomeChip key={i} outcome={o.outcome} />
          ))}
        </span>
      </summary>

      <div className="space-y-4 px-3 pt-1 pb-3">
        {trial.outcomes.map((outcome) => (
          <ExpectationBlock
            key={outcome.expectation_id}
            runId={runId}
            caseId={caseId}
            outcome={outcome}
            trial={trial}
            rulings={rulings}
            readOnly={readOnly}
          />
        ))}
        <UnmatchedFindings trial={trial} />
      </div>
    </details>
  )
}

function ExpectationBlock({
  runId,
  caseId,
  outcome,
  trial,
  rulings,
  readOnly,
}: {
  runId: string
  caseId: string
  outcome: ExpectationOutcome
  trial: TrialRecord
  rulings: Map<string, Dispute>
  readOnly: boolean
}) {
  const judged = new Set(outcome.verdicts.map((v) => v.finding_index))
  const unjudged = outcome.eligible_finding_indices.filter((i) => !judged.has(i))
  const excluded = excludedFindings(outcome, trial)

  return (
    <section className="border-l-2 border-line pl-3">
      <p className="flex flex-wrap items-baseline gap-2 text-sm">
        <OutcomeChip outcome={outcome.outcome} />
        <strong className="font-semibold">must {outcome.must.replace('_', ' ')}</strong>
        <span className="text-xs text-muted">({OUTCOME_TITLE[outcome.outcome]})</span>
      </p>

      <Expected outcome={outcome} />

      {outcome.verdicts.length === 0 && unjudged.length === 0 ? (
        <p className="mt-1 text-sm text-muted italic">
          No finding was eligible — nothing the reviewer reported reached the judge here.
        </p>
      ) : (
        <ul className="mt-2 space-y-2">
          {outcome.verdicts.map((v) => (
            <li key={v.finding_index}>
              <FindingLine finding={trial.findings[v.finding_index]} />
              <p className={`mt-0.5 ml-4 text-[13px] ${v.matched ? 'text-good' : 'text-bad'}`}>
                judge: {v.matched ? 'MATCHED' : 'NOT MATCHED'} (confidence {v.confidence.toFixed(2)}
                ) — {v.reason || 'no reason given'}
                {v.tier === 2 && (
                  <span
                    className="ml-2 rounded border border-line px-1 text-xs text-muted"
                    title="Tier 1 was unsure, so the pair was re-judged grounded in the case's own diff"
                  >
                    grounded
                  </span>
                )}
              </p>
              {v.prior && (
                <p className="mt-0.5 ml-4 text-xs text-muted">
                  tier 1 first said {v.prior.matched ? 'matched' : 'not matched'} at{' '}
                  {v.prior.confidence.toFixed(2)} — {v.prior.reason || 'no reason given'}
                </p>
              )}
              {/* Ruling needs the expectation snapshot to mint an honest pair; records that
                  predate snapshots get no controls rather than a button that always errors. */}
              {outcome.where != null && (
                <RulingLine
                  runId={runId}
                  request={{
                    case_id: caseId,
                    trial: trial.index,
                    expectation_id: outcome.expectation_id,
                    finding_index: v.finding_index,
                    is_match: false, // overwritten per click
                    note: '',
                  }}
                  existing={rulings.get(
                    rulingKey(caseId, trial.index, outcome.expectation_id, v.finding_index),
                  )}
                  readOnly={readOnly}
                />
              )}
            </li>
          ))}
          {unjudged.map((i) => (
            <li key={i}>
              <FindingLine finding={trial.findings[i]} />
              <p className="mt-0.5 ml-4 text-[13px] text-muted">
                not judged — an earlier finding already matched
              </p>
            </li>
          ))}
        </ul>
      )}

      {excluded.length > 0 && (
        <div className="mt-2">
          <p className="text-xs text-muted">Filtered out before judging:</p>
          <ul className="mt-1 space-y-1">
            {excluded.map(({ index, reason }) => (
              <li key={index}>
                <FindingLine finding={trial.findings[index]} />
                <p className="mt-0.5 ml-4 text-[13px] text-muted">not judged — {reason}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

/**
 * Rule on one judge verdict: same underlying issue, yes or no.
 *
 * Every ruling — agreeing with the judge or not — becomes a labeled pair in the judge's own eval
 * corpus. This is the only moment such a label is free: a person is already looking at the
 * verdict, deciding whether it was right, and without this control that judgment evaporates. The
 * two buttons ask about ground truth (do the finding and expectation describe the same issue?),
 * not about the judge; whether the judge agrees is derived and shown, not asked.
 */
function RulingLine({
  runId,
  request,
  existing,
  readOnly,
}: {
  runId: string
  request: DisputeRequest
  existing: Dispute | undefined
  readOnly: boolean
}) {
  const rule = useDisputeVerdict(runId)

  return (
    <div className="mt-1 ml-4 flex flex-wrap items-center gap-2 text-xs text-muted">
      {existing ? (
        <span
          title={`Ruled by ${existing.principal || 'unknown'} — feeds the judge's eval corpus. ${
            existing.note || ''
          }`}
        >
          ruled: {existing.is_match ? 'same issue' : 'different issue'}
          {existing.judge_matched === existing.is_match
            ? ' (judge was right)'
            : ' (judge was wrong)'}
        </span>
      ) : (
        <span title="Your ruling becomes a labeled pair the judge itself is scored against.">
          same underlying issue?
        </span>
      )}
      {!readOnly && (
        <>
          <RulingButton
            label="same"
            active={existing?.is_match === true}
            pending={rule.isPending}
            onClick={() => rule.mutate({ ...request, is_match: true })}
          />
          <RulingButton
            label="different"
            active={existing?.is_match === false}
            pending={rule.isPending}
            onClick={() => rule.mutate({ ...request, is_match: false })}
          />
        </>
      )}
      {rule.error != null && <span className="text-bad">{String(rule.error)}</span>}
    </div>
  )
}

function RulingButton({
  label,
  active,
  pending,
  onClick,
}: {
  label: string
  active: boolean
  pending: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      disabled={pending || active}
      onClick={onClick}
      className={`rounded border px-1.5 py-0.5 transition-colors ${
        active
          ? 'border-accent text-accent'
          : 'border-line hover:border-accent/50 hover:text-ink disabled:opacity-50'
      }`}
    >
      {label}
    </button>
  )
}

/** What the expectation asserted. Without it, "must appear · missed" is undiagnosable. */
function Expected({ outcome }: { outcome: ExpectationOutcome }) {
  const location = outcome.where
    ? outcome.where.path +
      (outcome.where.line_range
        ? ` lines ${outcome.where.line_range[0]}–${outcome.where.line_range[1]}`
        : '')
    : null

  if (!outcome.semantic && !location) {
    return (
      <p className="mt-1 text-xs text-muted italic">
        Expectation <code className="font-mono">{outcome.expectation_id}</code> — text not recorded
        by this run.
      </p>
    )
  }

  return (
    <div className="mt-1 text-sm">
      {outcome.semantic && <p className="text-muted">“{outcome.semantic}”</p>}
      <p className="text-xs text-muted">
        {location && <code className="font-mono">{location}</code>}
        {outcome.severity_min != null && (
          <span className="ml-2">severity ≥ {severityName(outcome.severity_min)}</span>
        )}
      </p>
    </div>
  )
}

const EXCLUSION_TEXT: Record<string, string> = {
  outside_region: 'outside the expected line range',
  below_severity: 'below the required severity',
}

/**
 * Findings the structural prefilter dropped. A reviewer that flagged the right line one severity
 * too low looks identical to a reviewer that said nothing — opposite problems, opposite fixes.
 */
function excludedFindings(outcome: ExpectationOutcome, trial: TrialRecord) {
  const where = outcome.where
  if (!where) return []
  const eligible = new Set(outcome.eligible_finding_indices)
  const out: { index: number; reason: string }[] = []

  trial.findings.forEach((finding, index) => {
    if (eligible.has(index) || finding.path !== where.path) return
    const inRegion =
      !where.line_range ||
      (finding.line != null &&
        finding.line >= where.line_range[0] &&
        finding.line <= where.line_range[1])
    const reason = !inRegion ? 'outside_region' : 'below_severity'
    out.push({ index, reason: EXCLUSION_TEXT[reason]! })
  })
  return out
}

/**
 * Findings that satisfied no expectation. These are the interesting ones: either an unlabelled true
 * positive worth promoting to a `should_catch` case, or noise worth pinning with `should_not_flag`.
 */
function UnmatchedFindings({ trial }: { trial: TrialRecord }) {
  const matched = new Set(
    trial.outcomes.flatMap((o) => o.verdicts.filter((v) => v.matched).map((v) => v.finding_index)),
  )
  // A finding already shown under an expectation as filtered-out has been explained in the place it
  // matters; repeating it here reads as two separate problems.
  const explained = new Set(
    trial.outcomes.flatMap((o) => excludedFindings(o, trial).map((e) => e.index)),
  )
  const unmatched = trial.findings
    .map((_, i) => i)
    .filter((i) => !matched.has(i) && !explained.has(i))
  if (unmatched.length === 0) return null

  return (
    <section className="border-l-2 border-line pl-3">
      <p className="text-sm">
        Findings matching no expectation{' '}
        <span className="text-xs text-muted">
          — candidates for a new eval case, either a missing <code>should_catch</code> or noise
          worth pinning with <code>should_not_flag</code>
        </span>
      </p>
      <ul className="mt-2 space-y-1">
        {unmatched.map((i) => (
          <li key={i}>
            <FindingLine finding={trial.findings[i]} />
          </li>
        ))}
      </ul>
    </section>
  )
}

function FindingLine({ finding }: { finding: Finding | undefined }) {
  if (!finding) return <span className="text-sm text-muted italic">finding missing</span>
  const location = finding.line === null ? finding.path : `${finding.path}:${finding.line}`
  return (
    <div className="flex flex-wrap items-baseline gap-2 text-sm">
      <code className="font-mono text-xs">{location}</code>
      <span className="text-xs text-muted">{severityName(finding.severity)}</span>
      {finding.rule_id && <code className="font-mono text-xs text-accent">{finding.rule_id}</code>}
      <span>{finding.message}</span>
      {finding.confidence != null && (
        <span className="text-xs text-muted">conf {finding.confidence.toFixed(2)}</span>
      )}
    </div>
  )
}

function isFlaky(caseRun: CaseRun): boolean {
  if (caseRun.trials.length < 2) return false
  const seen = new Map<string, Set<string>>()
  for (const trial of caseRun.trials) {
    for (const o of trial.outcomes) {
      const set = seen.get(o.expectation_id) ?? new Set<string>()
      set.add(o.outcome)
      seen.set(o.expectation_id, set)
    }
  }
  return [...seen.values()].some((s) => s.size > 1)
}
