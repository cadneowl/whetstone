import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  useConsoleConfig,
  useGraduate,
  useProposal,
  useSaveGuidance,
  type Job,
  type PendingCase,
  type SkillDetail as Detail,
} from '@/api/client'
import { GuidanceDiff } from '@/components/GuidanceDiff'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, ErrorNote, score } from '@/components/primitives'
import { SourceBadge } from '@/components/signals'

/** The `cases` param when the selection is deliberately empty — distinct from the param being
 *  absent, which means "everything" (the default a fresh visit starts on). */
const NONE = '-'

/**
 * Which cases the URL says are selected. Absent means all of them.
 *
 * Ids that are no longer pending — a stale link, or a case graduated since — are dropped rather
 * than carried into a request the server would refuse.
 */
export function selectionFrom(param: string | null, all: readonly string[]): ReadonlySet<string> {
  if (param === null) return new Set(all)
  if (param === NONE || param === '') return new Set()
  const known = new Set(all)
  return new Set(param.split(',').filter((id) => known.has(id)))
}

/** The inverse: `null` to drop the param entirely, which is how "all" stays out of the URL. */
export function selectionParam(
  selected: ReadonlySet<string>,
  all: readonly string[],
): string | null {
  if (selected.size === all.length && all.every((id) => selected.has(id))) return null
  if (selected.size === 0) return NONE
  return all.filter((id) => selected.has(id)).join(',')
}

/**
 * The improve workspace: one place to take a skill from "triage promoted some cases it fails on"
 * to a gate-proven guidance change, ready for you to commit.
 *
 * The console edits in place: the skill files on disk are the artifact — edited by hand in your own
 * editor, or by the LLM sharpen below, both writing straight to `skills/<id>/`. It never touches
 * git; you commit, branch and push the result yourself. Every action is scoped to the promoted cases
 * you select.
 *
 * The selection and the batch run live in the URL rather than in component state, because step 2
 * sends you to the Edit tab to hand-edit and you have to be able to come back to where you were —
 * and because a workspace whose state cannot be linked to cannot be handed to a colleague.
 */
export function ImproveWorkspace({ detail }: { detail: Detail }) {
  const skillId = detail.skill.id
  const pending = detail.pending_cases
  const { data: config } = useConsoleConfig()
  const { data: proposal } = useProposal(skillId)
  const save = useSaveGuidance()
  const graduate = useGraduate(skillId)
  const readOnly = Boolean(config?.read_only)

  const [params, setParams] = useSearchParams()
  const ids = pending.map((c) => c.id)
  const selected = selectionFrom(params.get('cases'), ids)
  const batchRun = params.get('run')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [notice, setNotice] = useState('')
  const [instruction, setInstruction] = useState('')

  /** Write workspace state back to the query, leaving `tab` and anything else alone. */
  const patch = (changes: Record<string, string | null>) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        for (const [key, value] of Object.entries(changes)) {
          if (value === null) next.delete(key)
          else next.set(key, value)
        }
        return next
      },
      { replace: true },
    )

  const setSelected = (next: ReadonlySet<string>) =>
    patch({ cases: selectionParam(next, ids) })

  const toggle = (id: string) => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelected(next)
  }

  // Where the Edit tab's full editor lives, with the workspace's state carried across so coming
  // back lands on the same score and the same ticked cases.
  const editorSearch = (() => {
    const next = new URLSearchParams(params)
    next.set('tab', 'edit')
    return `?${next.toString()}`
  })()

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
  // The same blindfold blocks the *improve* step, which is where it actually hurt: a lone promoted
  // case that hashes into the holdout could be scored, seen to miss, and then handed to a drafter
  // forbidden from looking at it — one wasted call and a draft that changed nothing.
  const noneDraftable = selected.size > 0 && heldSelected.length === selected.size

  // Corpus cases the last run got wrong — exactly what the no-batch improve step drafts from, so
  // exactly what a gate on that draft should be made to prove. Holdout cases are excluded: a change
  // may not claim to fix a case the improve loop was never shown, and the gate refuses such a claim.
  const failedLastRun = detail.cases
    .filter((c) =>
      c.kind === 'should_catch'
        ? c.last_recall != null && c.last_recall < 1
        : c.last_fp_rate != null && c.last_fp_rate > 0,
    )
    .filter((c) => !c.holdout)
    .map((c) => c.id)

  // A strict subset scores just those cases — the cheap, targeted check. All (or none) selected
  // scores the whole promoted set. Neither touches the graduated corpus unless the second button
  // is used, so regressions come from there or from the gate.
  // `scoreKey` resets the launch button's cost plan whenever the selection changes.
  const scoreSubset = selected.size > 0 && selected.size < pending.length
  const scoreKey = scoreSubset ? [...selected].sort().join(',') : 'all'

  // Both score buttons land the same way — the run they produce is what the sharpen step drafts
  // from, whether or not the corpus was underneath it.
  const scored = (job: Job) => {
    const r = job.result as Record<string, unknown>
    patch({ run: String(r.run_id ?? '') || null })
    setNotice(
      `Scored: recall ${fmt(r.recall)} · fp ${fmt(r.fp_rate)}` +
        (r.run_id ? ` — open the run to see each case.` : ''),
    )
  }

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
    // No inner measure: this is a workbench, not prose. It sat at `max-w-3xl` inside the skill
    // page's own `max-w-6xl`, so two thirds of a wide window was empty while the case rows — which
    // carry a checkbox, three badges, a path and two controls — wrapped for want of room.
    <div className="space-y-5">
      <InPlaceNotice skillId={skillId} />

      {hasBatch && (
        <section className="rounded-lg border border-line bg-surface p-4">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-medium">
              Proposed cases ({selected.size} of {pending.length} selected)
            </h3>
            <div className="flex gap-3 text-xs text-muted">
              <button type="button" onClick={() => setSelected(new Set(ids))}>
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
              Missed / falsely-flagged cases are what to sharpen next. Add the corpus to see
              regressions too — it costs every graduated case, which is why it is the second
              button rather than the only one.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <LaunchButton
                key={scoreKey}
                kind="eval"
                request={{
                  skill_id: skillId,
                  scope: 'promoted',
                  ...(scoreSubset ? { cases: [...selected] } : {}),
                }}
                label={scoreSubset ? `Score ${selected.size} selected` : 'Score the promoted batch'}
                onDone={scored}
              />
              <LaunchButton
                key={`corpus-${scoreKey}`}
                kind="eval"
                request={{
                  skill_id: skillId,
                  scope: 'promoted',
                  with_corpus: true,
                  ...(scoreSubset ? { cases: [...selected] } : {}),
                }}
                label="…with the eval corpus too"
                onDone={scored}
              />
            </div>
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
              {/* Both paths, and both reachable. This used to offer hand-editing in prose and only
                  the LLM in fact — the editor is on another tab and nothing here said so, let alone
                  linked to it. */}
              <p className="mt-0.5 mb-2 text-xs text-muted">
                Draft a change with the LLM from the cases you selected, or{' '}
                <Link to={{ search: editorSearch }} className="text-accent underline">
                  edit the files by hand
                </Link>{' '}
                in the Edit tab (your ticked cases and this score come back with you). Either way it
                lands on disk and is re-scored above.
              </p>
              {/* Every ticked case is holdout, so the drafter would be shown nothing at all and
                  the call could only return the guidance unchanged. The server refuses this at
                  plan time now; disabling it here means the operator never gets as far as a cost
                  banner for a draft that cannot happen. Selecting nothing is *not* this case — an
                  empty selection means "every failure in the run". */}
              {noneDraftable && (
                <p className="mb-2 text-xs text-warn">
                  {heldSelected.map((c) => c.id).join(', ')}{' '}
                  {heldSelected.length === 1 ? 'is the only ticked case, and it is' : 'are all'}{' '}
                  holdout — scored on every run, never shown to the drafter, so there is nothing
                  here to sharpen from. Tick a case outside the holdout, or, if this corpus is too
                  small for a holdout to measure anything, set{' '}
                  <code className="font-mono">sample.holdout_fraction: 0</code> in this
                  skill&rsquo;s evaluate <code className="font-mono">step.yaml</code> and score
                  again.
                </p>
              )}
              <LaunchButton
                kind="improve"
                request={{ skill_id: skillId, run_id: batchRun, cases: [...selected], instruction }}
                label={selected.size ? 'Improve from selected' : 'Improve from every failure'}
                disabled={noneDraftable}
                disabledReason="Every ticked case is holdout — the drafter is never shown one."
                onDone={onDrafted}
              >
                {/* An empty selection is not an empty draft: the server reads no ids as "no
                    filter" and learns from every failure in the run. Saying "drafts from the 0
                    selected case(s)" described the opposite of what the button does — and sat
                    directly above a cost banner correctly saying "up to N clustered failure(s)". */}
                <p className="mb-2 text-xs text-muted">
                  {selected.size
                    ? `Drafts from the ${selected.size} selected case(s)`
                    : 'Nothing is ticked, so this drafts from every failure in the run — tick cases above to narrow it'}
                  {batchRun ? '' : ' — score them first for the drafter to see what fails'}.
                </p>
                <Steer value={instruction} onChange={setInstruction} />
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
                  request={{ skill_id: skillId, run_id: latestRunId, instruction }}
                  label="Draft from the last run"
                  onDone={onDrafted}
                >
                  <p className="mb-2 text-xs text-muted">
                    Drafts from every case the last run failed. For a finer, multi-file hand edit,{' '}
                    <Link to={{ search: editorSearch }} className="text-accent underline">
                      the Edit tab
                    </Link>{' '}
                    has the full editor.
                  </p>
                  <Steer value={instruction} onChange={setInstruction} />
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
          {/* Without a batch this used to send no targets at all, so the commonest sharpening loop
              — improve from what the last run failed, then gate — proved only that nothing broke.
              A gate that claims nothing is a rot guard; naming the cases is what makes it evidence
              of an improvement. */}
          {!hasBatch && failedLastRun.length > 0 && (
            <p className="mb-2 text-xs text-muted">
              Targeting the {failedLastRun.length} case(s) the last run failed — they must pass, so
              this gate proves the change fixed something rather than merely broke nothing.
            </p>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <LaunchButton
              key={(hasBatch ? targetable : failedLastRun).join(',')}
              kind="gate"
              request={{ skill_id: skillId, targeted: hasBatch ? targetable : failedLastRun }}
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

/**
 * The free-text steer on an improve run.
 *
 * Not a nicety: when the selected cases all pass, the cost plan says *"there is nothing to fix —
 * add an instruction if you want it rewritten anyway"*, and this workspace had no instruction to
 * add. The advice named a control that existed only on another tab.
 */
function Steer({ value, onChange }: { value: string; onChange: (next: string) => void }) {
  return (
    <label className="block text-xs text-muted">
      Steer this run (optional)
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. focus on false positives in test files"
        className="mt-1 w-full max-w-xl rounded border border-line bg-surface px-2 py-1 text-sm text-ink outline-none focus:border-accent/60"
      />
    </label>
  )
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
