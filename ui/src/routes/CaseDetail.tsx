import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  useCase,
  useConsoleConfig,
  useDeleteCase,
  useEditCase,
  type CaseDetail as CaseDetailData,
  type CaseTier,
  type EvalKind,
} from '@/api/client'
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
  const { data: config } = useConsoleConfig()
  const [editing, setEditing] = useState(false)
  const readOnly = Boolean(config?.read_only)

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  const { case: evalCase, diff, history, baseline, promoted, sidecars } = data
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
          {/* Scored by every batch eval and gate, but with no file under `eval_cases/` yet — so the
              editors below have nothing to write to. Said here rather than left for the operator to
              discover as a "no eval case" error on save. */}
          {promoted && (
            <Badge
              tone="warn"
              title="Promoted from triage and scored, but not yet graduated into the eval corpus. Graduate it from the skill's Inbox to edit or archive it here."
            >
              promoted, not graduated
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
            <div className="mb-2 flex items-baseline justify-between">
              <h2 className="text-xs tracking-wide text-muted uppercase">Expectations</h2>
              {!readOnly && (
                <button
                  type="button"
                  onClick={() => setEditing(!editing)}
                  className="text-xs text-accent hover:underline"
                >
                  {editing ? 'close' : 'edit'}
                </button>
              )}
            </div>
            {editing && (
              <CaseEditor skillId={skillId} evalCase={evalCase} onDone={() => setEditing(false)} />
            )}
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

          <LocalContextRead sidecars={sidecars} />
        </aside>
      </div>
    </div>
  )
}

/**
 * The `.agents/` notes the last run gave this case's reviewer.
 *
 * This page is where "why did it miss this?" is asked, and local context is one of the two
 * answers. "The reviewer never loaded the note" and "it read the note and disagreed" are opposite
 * diagnoses that looked identical here — the record has carried the answer since the feature
 * shipped and no screen showed it.
 *
 * Absent entirely for a skill that declares no role, which is most of them. An empty panel reading
 * "0 files" would suggest a broken tree on every ordinary skill in the deployment.
 */
function LocalContextRead({ sidecars }: { sidecars: CaseDetailData['sidecars'] }) {
  if (!sidecars) return null
  const observed = sidecars.resolved_by === 'reviewer'
  return (
    <section>
      <h2 className="mb-2 text-xs tracking-wide text-muted uppercase">Local context</h2>
      {sidecars.paths.length === 0 ? (
        <p className="text-sm text-warn">
          {observed
            ? 'The reviewer opened none of the notes for this case’s folders.'
            : 'No folder this case touches keeps notes for this role.'}
        </p>
      ) : (
        <ul className="space-y-1">
          {sidecars.paths.map((path) => (
            <li key={path} className="font-mono text-xs break-all text-muted">
              {path}
            </li>
          ))}
        </ul>
      )}
      {sidecars.dropped.length > 0 && (
        <p className="mt-2 text-xs text-warn">
          {sidecars.dropped.length} matched but did not reach the prompt — the caps dropped the most
          general folders first.
        </p>
      )}
      {sidecars.missing.length > 0 && (
        <p className="mt-2 text-xs text-warn">
          {sidecars.missing.length} folder(s) this case names are not in the source tree, so the
          reviewer could not have had local context for them however good the notes elsewhere are.
        </p>
      )}
      {/* The distinction the whole field turns on. An observation is a lower bound, and a reader
          who treats it as the complete set will conclude a note that exists was never written. */}
      <p className="mt-2 text-xs text-muted">
        {observed
          ? 'Observed: this skill’s own reviewer collects its context, so this is what it was seen to open and it may have read more. Not hashed.'
          : 'Resolved by the harness before the call — the complete set, and part of this run’s identity.'}
      </p>
    </section>
  )
}

/**
 * Correct or remove a case that has already graduated.
 *
 * A case became permanent the moment it entered the corpus: the console could show it, flip its
 * tier, and nothing else. But the wording of an expectation *is* the measurement — a typo in one
 * is a wrong measurement, and "archive it" is not a fix, it is a smaller wrong measurement.
 *
 * Both actions change `skill_hash`, which retracts the gate verdict. Said on the panel rather than
 * left to be discovered when Propose refuses.
 */
function CaseEditor({
  skillId,
  evalCase,
  onDone,
}: {
  skillId: string
  evalCase: CaseDetailData['case']
  onDone: () => void
}) {
  const first = evalCase.expect[0]
  const edit = useEditCase(skillId, evalCase.id)
  const remove = useDeleteCase(skillId)
  const navigate = useNavigate()
  const [semantic, setSemantic] = useState(first?.semantic ?? '')
  const [kind, setKind] = useState<EvalKind>(evalCase.kind)
  const [tier, setTier] = useState<CaseTier>(evalCase.tier ?? 'active')
  const [confirming, setConfirming] = useState(false)

  return (
    <div className="mb-3 space-y-2 rounded-lg border border-accent/30 bg-accent/5 p-3">
      <label className="block text-xs text-muted">
        Expectation — the ground truth every finding is judged against
        <textarea
          value={semantic}
          onChange={(e) => setSemantic(e.target.value)}
          rows={4}
          className="mt-1 w-full rounded border border-line bg-canvas px-2 py-1 text-sm text-ink outline-none focus:border-accent/60"
        />
      </label>
      <div className="flex flex-wrap items-end gap-3 text-xs text-muted">
        <label>
          Kind
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as EvalKind)}
            className="ml-2 rounded border border-line bg-canvas px-2 py-1 text-ink"
          >
            <option value="should_catch">should catch</option>
            <option value="should_not_flag">should not flag</option>
          </select>
        </label>
        <label title="Archive keeps the case as regression insurance but draws it at low weight">
          Tier
          <select
            value={tier}
            onChange={(e) => setTier(e.target.value as CaseTier)}
            className="ml-2 rounded border border-line bg-canvas px-2 py-1 text-ink"
          >
            <option value="active">active</option>
            <option value="archive">archive</option>
          </select>
        </label>
        <span className="ml-auto flex gap-2">
          <button
            type="button"
            disabled={edit.isPending || !semantic.trim()}
            onClick={() =>
              edit.mutate(
                {
                  semantic,
                  kind,
                  tier,
                  severity_min: first?.severity_min ?? null,
                  line_range: first?.where.line_range ?? null,
                },
                { onSuccess: onDone },
              )
            }
            className="rounded border border-accent/50 px-2 py-1 text-accent transition-colors hover:bg-accent/10 disabled:opacity-40"
          >
            {edit.isPending ? 'Saving…' : 'Save'}
          </button>
          <button type="button" onClick={onDone} className="px-2 py-1 hover:text-ink">
            Cancel
          </button>
        </span>
      </div>
      <p className="text-xs text-warn">
        Editing changes what this skill is measured against, so the gate verdict is retracted —
        re-gate before proposing.
      </p>
      <div className="flex items-center gap-2 border-t border-line pt-2 text-xs">
        {confirming ? (
          <>
            <span className="text-warn">
              Remove this case from the corpus? Archive instead if it is still evidence.
            </span>
            <button
              type="button"
              disabled={remove.isPending}
              onClick={() =>
                remove.mutate(evalCase.id, {
                  onSuccess: () => navigate(`/skills/${encodeURIComponent(skillId)}`),
                })
              }
              className="rounded border border-bad/50 px-2 py-0.5 text-bad transition-colors hover:bg-bad/10 disabled:opacity-40"
            >
              {remove.isPending ? 'Removing…' : 'Yes, remove'}
            </button>
            <button type="button" onClick={() => setConfirming(false)} className="text-muted">
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="text-muted transition-colors hover:text-bad"
          >
            Remove this case from the corpus
          </button>
        )}
      </div>
      {edit.error != null && <ErrorNote error={edit.error} />}
      {remove.error != null && <ErrorNote error={remove.error} />}
    </div>
  )
}
