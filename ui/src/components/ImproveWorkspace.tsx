import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  useConsoleConfig,
  useGraduate,
  useProposal,
  useSaveGuidance,
  type PendingCase,
  type SkillDetail as Detail,
} from '@/api/client'
import { GuidanceDiff } from '@/components/GuidanceDiff'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, ErrorNote, score } from '@/components/primitives'
import { SourceBadge } from '@/components/signals'

/**
 * The improve workspace: one place to take a skill from "triage promoted some cases it fails on"
 * to a gate-proven guidance change, ready for you to commit.
 *
 * The console edits in place: the skill files on disk are the artifact — edited by hand in your own
 * editor, or by the LLM sharpen below, both writing straight to `skills/<id>/`. It never touches
 * git; you commit, branch and push the result yourself. Every action is scoped to the promoted cases
 * you select.
 */
export function ImproveWorkspace({ detail }: { detail: Detail }) {
  const skillId = detail.skill.id
  const pending = detail.pending_cases
  const { data: config } = useConsoleConfig()
  const { data: proposal } = useProposal(skillId)
  const save = useSaveGuidance()
  const graduate = useGraduate(skillId)
  const readOnly = Boolean(config?.read_only)

  const [selected, setSelected] = useState<ReadonlySet<string>>(
    () => new Set(pending.map((c) => c.id)),
  )
  const [batchRun, setBatchRun] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [notice, setNotice] = useState('')

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  // The batch is the pre-commit sharpening path: promote cases in triage, then close the gap here.
  // Without one, the same loop still runs against the corpus cases the last run failed — which is
  // exactly the state the inbox's "improve" action sends you here in. So the tab is a hub, not a
  // dead end: sharpen and gate are always present; only their fuel changes.
  const hasBatch = pending.length > 0
  const latestRunId = detail.runs[0]?.id ?? null

  // Holdout cases are scored but can never be gate-targeted (a change may not claim to fix a case
  // the improve loop never saw). Keep them out of the gate's targeted set so it is never refused,
  // and say why rather than let the run fail after minutes of model calls.
  const heldSelected = pending.filter((c) => selected.has(c.id) && c.holdout)
  const targetable = pending.filter((c) => selected.has(c.id) && !c.holdout).map((c) => c.id)

  // A strict subset scores just those cases — the cheap, targeted check. All (or none) selected
  // scores the whole promoted set, so a regression on a case you did not pick still shows.
  // `scoreKey` resets the launch button's cost plan whenever the selection changes.
  const scoreSubset = selected.size > 0 && selected.size < pending.length
  const scoreKey = scoreSubset ? [...selected].sort().join(',') : 'all'

  const applyDraft = () =>
    draft &&
    save.mutate(
      { skillId, edit: { body: draft.body, pages: draft.pages } },
      {
        onSuccess: () => {
          setDraft(null)
          setNotice('Applied to the skill files on disk. Re-score to see if it caught them.')
        },
      },
    )
  const onDrafted = (job: { result?: unknown }) => {
    const r = (job.result ?? {}) as Record<string, unknown>
    const body = String(r.body ?? '')
    const pages = (r.pages ?? {}) as Record<string, string>
    if (!body && !Object.keys(pages).length) {
      setNotice('The drafter proposed no change.')
      return
    }
    setDraft({
      body,
      pages,
      rationale: String(r.rationale ?? ''),
      selectedMissing: (r.selected_missing ?? []) as string[],
    })
  }

  const review = draft && (
    <DraftReview
      draft={draft}
      // The on-disk guidance the draft would replace, so every rewritten file shows as a diff.
      baseline={{ body: proposal?.body ?? '', pages: proposal?.pages ?? {} }}
      applying={save.isPending}
      readOnly={readOnly}
      error={save.error}
      onApply={applyDraft}
      onDiscard={() => setDraft(null)}
    />
  )

  // Step numbers depend on whether the batch-score step is present, so the sharpen and gate steps
  // read as "1 · 2" without a batch and "1 · 2 · 3" with one.
  const step = hasBatch ? { sharpen: '2', gate: '3' } : { sharpen: '1', gate: '2' }

  return (
    <div className="max-w-3xl space-y-5">
      <InPlaceNotice skillId={skillId} />

      {hasBatch && (
        <section className="rounded-lg border border-line bg-surface p-4">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-medium">
              Proposed cases ({selected.size} of {pending.length} selected)
            </h3>
            <div className="flex gap-3 text-xs text-muted">
              <button type="button" onClick={() => setSelected(new Set(pending.map((c) => c.id)))}>
                all
              </button>
              <button
                type="button"
                onClick={() => setSelected(new Set(pending.filter(isFailing).map((c) => c.id)))}
                title="Select only the cases the last score got wrong"
              >
                failing
              </button>
              <button type="button" onClick={() => setSelected(new Set())}>
                none
              </button>
            </div>
          </div>
          {/* The checkboxes drive the score (a strict subset scores just those, all-or-none scores
              the whole batch so regressions still show), the LLM sharpen, and the gate's targets. */}
          <p className="mb-3 text-xs text-muted">
            The checkboxes drive what gets scored, sharpened and gate-targeted. Tick a few to score
            just those; leave all (or none) ticked to score the whole batch, so a regression
            elsewhere still shows. Caught / missed is from the latest score.
            <strong> Graduate</strong> a case once you&rsquo;re satisfied it belongs — that moves it
            into the eval corpus (only some earn it), which needs a fresh gate afterwards.
          </p>
          <ul className="space-y-1.5">
            {pending.map((c) => (
              <li key={c.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
                <input
                  type="checkbox"
                  checked={selected.has(c.id)}
                  onChange={() => toggle(c.id)}
                  aria-label={`select ${c.id}`}
                />
                <Badge tone={c.kind === 'should_catch' ? 'accent' : 'neutral'}>
                  {c.kind === 'should_catch' ? 'should catch' : 'should not flag'}
                </Badge>
                <SourceBadge provenance={c.provenance} />
                <span className="font-mono text-xs text-muted">{c.path}</span>
                {c.holdout && (
                  <Badge
                    tone="neutral"
                    title="Holdout — scored on every run, but the gate can't target it and the improve loop never learns from it"
                  >
                    holdout
                  </Badge>
                )}
                <span className="ml-auto flex items-center gap-2">
                  <CaseStatus c={c} />
                  <button
                    type="button"
                    disabled={readOnly || graduate.isPending}
                    onClick={() =>
                      graduate.mutate(c.id, {
                        onSuccess: () =>
                          setNotice(
                            `Graduated ${c.id} into the eval corpus — re-gate before you commit.`,
                          ),
                      })
                    }
                    title="Move this case from promoted_cases/ into the eval corpus. Changes skill_hash, so a fresh gate is needed."
                    className="rounded border border-good/50 px-2 py-0.5 text-xs text-good transition-colors hover:bg-good/10 disabled:opacity-40"
                  >
                    Graduate
                  </button>
                </span>
              </li>
            ))}
          </ul>
          {graduate.error != null && <ErrorNote error={graduate.error} />}
        </section>
      )}

      <section className="space-y-3 rounded-lg border border-line bg-surface p-4">
        {hasBatch && (
          <div>
            <h3 className="text-sm font-medium">1 · Score the promoted batch</h3>
            <p className="mt-0.5 mb-2 text-xs text-muted">
              {scoreSubset
                ? `Runs the on-disk guidance over the ${selected.size} case(s) you ticked above.`
                : 'Runs the on-disk guidance over every promoted case.'}{' '}
              Missed / falsely-flagged cases are what to sharpen next; a regressed case is what to
              protect.
            </p>
            <LaunchButton
              key={scoreKey}
              kind="eval"
              request={{
                skill_id: skillId,
                scope: 'promoted',
                ...(scoreSubset ? { cases: [...selected] } : {}),
              }}
              label={scoreSubset ? `Score ${selected.size} selected` : 'Score the promoted batch'}
              onDone={(job) => {
                const r = job.result as Record<string, unknown>
                setBatchRun(String(r.run_id ?? '') || null)
                setNotice(
                  `Scored: recall ${fmt(r.recall)} · fp ${fmt(r.fp_rate)}` +
                    (r.run_id ? ` — open the run to see each case.` : ''),
                )
              }}
            />
            {batchRun && (
              <Link
                to={`/runs/${encodeURIComponent(batchRun)}`}
                className="ml-3 text-xs text-accent underline"
              >
                open the run →
              </Link>
            )}
          </div>
        )}

        <div className={hasBatch ? 'border-t border-line pt-3' : undefined}>
          <h3 className="text-sm font-medium">{step.sharpen} · Sharpen the guidance</h3>
          {hasBatch ? (
            <>
              <p className="mt-0.5 mb-2 text-xs text-muted">
                Edit the skill files on disk by hand, or draft a change with the LLM from the cases
                you selected. Either way it lands on disk and is re-scored above.
              </p>
              <LaunchButton
                kind="improve"
                request={{ skill_id: skillId, run_id: batchRun, cases: [...selected] }}
                label="Improve from selected"
                onDone={onDrafted}
              >
                <p className="text-xs text-muted">
                  Drafts from the {selected.size} selected case(s)
                  {batchRun ? '' : ' — score them first for the drafter to see what fails'}.
                </p>
              </LaunchButton>
            </>
          ) : (
            <>
              <p className="mt-0.5 mb-2 text-xs text-muted">
                No promoted batch waiting — sharpening runs against the corpus cases the last run got
                wrong. Draft with the LLM here, or edit the files on disk by hand. To sharpen against
                fresh signal instead,{' '}
                <Link to="/triage" className="underline">
                  promote cases in Triage
                </Link>
                .
              </p>
              {latestRunId ? (
                <LaunchButton
                  kind="improve"
                  request={{ skill_id: skillId, run_id: latestRunId }}
                  label="Draft from the last run"
                  onDone={onDrafted}
                >
                  <p className="text-xs text-muted">
                    Drafts from every case the last run failed. For a finer, multi-file hand edit,
                    the <em>Edit</em> tab has the full editor.
                  </p>
                </LaunchButton>
              ) : (
                <p className="text-xs text-muted italic">
                  Never scored — run evals from the header first, so the drafter can see what the
                  guidance currently gets wrong.
                </p>
              )}
            </>
          )}
          {review}
        </div>

        <div className="border-t border-line pt-3">
          <h3 className="text-sm font-medium">{step.gate} · Gate &amp; commit</h3>
          <p className="mt-0.5 mb-2 text-xs text-muted">
            When the cases pass and nothing regressed, prove it: the gate scores your last commit
            against what&rsquo;s on disk over the union
            {hasBatch ? ', and the cases you selected must pass' : ''}. A brand-new skill (nothing
            committed yet) is scored against the naked model instead.
          </p>
          {heldSelected.length > 0 && (
            <p className="mb-2 text-xs text-muted">
              {heldSelected.map((c) => c.id).join(', ')} {heldSelected.length === 1 ? 'is' : 'are'}{' '}
              holdout — scored, but not gate-targeted (a change can&rsquo;t claim to fix a case the
              improve loop never sees).
            </p>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <LaunchButton
              kind="gate"
              request={{ skill_id: skillId, targeted: targetable }}
              label="Run the gate"
            />
            {proposal?.verdict.can_propose ? (
              <Badge tone="good" title="A passing gate covers the exact guidance on disk.">
                gate-proven ✓
              </Badge>
            ) : (
              <Badge tone="warn" title={proposal?.verdict.reason}>
                not gated yet
              </Badge>
            )}
          </div>
          {/* The console never commits or pushes. Guidance and cases both ship through your own git:
              a guidance change should carry a passing gate; adding cases needs no gate (it can only
              test the reviewer harder). */}
          <p className="mt-2 text-xs text-muted">
            The console never touches git. When it&rsquo;s gate-proven,{' '}
            <strong>commit and push it yourself</strong> — guidance carrying the passing gate,
            graduated <code className="font-mono">eval_cases/</code> alongside (adding a case needs
            no gate).
          </p>
        </div>
      </section>

      {notice && (
        <p className="rounded-lg border border-good/40 bg-good/5 px-3 py-2 text-sm break-words">
          {notice}
        </p>
      )}
    </div>
  )
}

type Draft = {
  body: string
  pages: Record<string, string>
  rationale: string
  selectedMissing: string[]
}

function InPlaceNotice({ skillId }: { skillId: string }) {
  return (
    <section className="rounded-lg border border-warn/40 bg-warn/5 p-4">
      <h3 className="text-sm font-medium">Edits happen in place</h3>
      <p className="mt-0.5 text-xs text-muted">
        Everything here — your hand edits and the LLM&rsquo;s drafts — writes straight to{' '}
        <span className="font-mono">skills/{skillId}/</span> on disk. The console never creates a
        branch, commits, or pushes: when a change is gate-proven, commit and push it with your own
        git. Compare against your last commit with <code className="font-mono">git diff</code>, or
        gate it below.
      </p>
    </section>
  )
}

function DraftReview({
  draft,
  baseline,
  applying,
  readOnly,
  error,
  onApply,
  onDiscard,
}: {
  draft: Draft
  // The on-disk guidance the draft would replace — body plus every companion page, keyed by path —
  // so each rewritten file is shown as a diff rather than a wall of new text.
  baseline: { body: string; pages: Record<string, string> }
  applying: boolean
  readOnly: boolean
  error: unknown
  onApply: () => void
  onDiscard: () => void
}) {
  // Every file the draft actually rewrites: SKILL.md when the body moved, plus each companion page
  // the drafter returned (the server already drops pages handed back unchanged). A skill is a
  // folder and the improve step edits it as one, so the review shows it as one — not just the body.
  const files = [
    ...(draft.body.trim() !== baseline.body.trim()
      ? [{ path: 'SKILL.md', before: baseline.body, after: draft.body }]
      : []),
    ...Object.entries(draft.pages).map(([path, after]) => ({
      path,
      before: baseline.pages[path] ?? '',
      after,
    })),
  ]
  return (
    <div className="mt-3 space-y-2 rounded-lg border border-accent/40 bg-accent/5 p-3">
      <p className="text-xs text-muted">
        Drafted a change to {files.length} file{files.length === 1 ? '' : 's'}. Read it before
        applying — the drafter is not the reviewer.
      </p>
      {draft.rationale && <p className="text-sm">{draft.rationale}</p>}
      {files.length === 0 ? (
        <p className="text-xs text-warn">The drafter returned no change to any file.</p>
      ) : (
        <div className="space-y-3">
          {files.map((f) => (
            <div key={f.path} className="space-y-1">
              <p className="font-mono text-xs text-muted">{f.path}</p>
              <GuidanceDiff before={f.before} after={f.after} />
            </div>
          ))}
        </div>
      )}
      {draft.selectedMissing.length > 0 && (
        <p className="text-xs text-warn">
          Not drafted from: {draft.selectedMissing.join(', ')} — the score did not fail them (or they
          are holdout), so they were not shown to the drafter.
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onApply}
          disabled={readOnly || applying || files.length === 0}
          className="rounded border border-good/50 px-3 py-1 text-sm text-good hover:bg-good/10 disabled:opacity-40"
        >
          {applying ? 'Applying…' : 'Apply to disk'}
        </button>
        <button type="button" onClick={onDiscard} className="px-2 py-1 text-sm text-muted">
          Discard
        </button>
      </div>
      {error != null && <ErrorNote error={error} />}
    </div>
  )
}

/** Caught / missed for one pending case, read from its latest score. */
function CaseStatus({ c }: { c: PendingCase }) {
  if (c.kind === 'should_catch') {
    if (c.last_recall == null) return <span className="text-xs text-muted italic">not scored</span>
    return c.last_recall >= 1 ? (
      <Badge tone="good">caught</Badge>
    ) : (
      <Badge tone="warn">missed {score(c.last_recall, 2)}</Badge>
    )
  }
  if (c.last_fp_rate == null) return <span className="text-xs text-muted italic">not scored</span>
  return c.last_fp_rate <= 0 ? (
    <Badge tone="good">clean</Badge>
  ) : (
    <Badge tone="warn">flagged {score(c.last_fp_rate, 2)}</Badge>
  )
}

function isFailing(c: PendingCase): boolean {
  return c.kind === 'should_catch'
    ? c.last_recall != null && c.last_recall < 1
    : c.last_fp_rate != null && c.last_fp_rate > 0
}

function fmt(v: unknown): string {
  return typeof v === 'number' ? v.toFixed(2) : '—'
}
