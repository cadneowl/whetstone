import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  useConsoleConfig,
  useGraduate,
  useProposal,
  useSaveGuidance,
  type Job,
  type JobRequest,
  type PendingCase,
  type SkillDetail as Detail,
} from '@/api/client'
import { GuidanceDiff } from '@/components/GuidanceDiff'
import { ImprovePromptPanel } from '@/components/ImprovePrompt'
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

/**
 * The `cases` to narrow an improve to, or `null` to send none and let it draft from every failure.
 *
 * Only a *strict* subset narrows. Everything ticked is the state a fresh visit starts in, and
 * reading it as "draft from exactly these" is what made a corpus regression undraftable: the batch
 * was the whole tick list, so every other failure in the run was silently dropped — and when the
 * batch had meanwhile started passing, the step refused for having nothing to work from at all.
 */
export function narrowedCases(
  selected: ReadonlySet<string>,
  all: readonly string[],
): string[] | null {
  if (selected.size === 0 || selected.size >= all.length) return null
  return all.filter((id) => selected.has(id))
}

/**
 * What the sharpen button calls itself, and what it says it will draft from.
 *
 * Keyed on `narrowed` — the value actually sent — rather than on the tick count, because those two
 * disagree in a state an operator reaches by doing nothing at all: a fresh visit starts with every
 * case ticked, and `narrowedCases` reads that as "no filter". A tick count in the label therefore
 * promised a narrowing the request did not carry.
 *
 * Exported and pure because this is the claim the panel makes about what it is about to spend on,
 * and it was wrong twice over before: the no-batch path — the common one — hardcoded "Drafts from
 * every case the last run failed" while `improveRequest` carried the selection regardless, flatly
 * contradicting the checkbox panel directly above it.
 *
 * Three scope states, not two: "every failure" is reached both by ticking nothing and by ticking
 * everything, and only the first is fixed by ticking something.
 */
export function sharpenWording(
  narrowed: readonly string[] | null,
  ticked: number,
  hasBatch: boolean,
  distill = false,
): { label: string; scope: string } {
  // A distill is a different job from the one every other label here describes. It consolidates
  // guidance the corpus has stopped standing behind, and the failures it happens to also see are
  // beside the point — so a button reading "Draft from the last run" would name the part that
  // matters least, on the one run where a rule can leave the guidance unnoticed.
  if (distill) {
    return {
      label: 'Distill the guidance',
      // No trailing period: the panel adds one, the way every other scope string here relies on.
      scope:
        'Consolidates the rules no eval case is linked to, alongside ' +
        (narrowed ? `the ${narrowed.length} selected case(s)` : 'whatever the last run got wrong') +
        ' — the draft will name every rule it removes',
    }
  }
  if (narrowed) {
    return {
      label: hasBatch ? 'Improve from selected' : 'Draft from selected',
      scope: `Drafts from the ${narrowed.length} selected case(s)`,
    }
  }
  return {
    label: hasBatch ? 'Improve from every failure' : 'Draft from the last run',
    scope:
      ticked === 0
        ? 'Nothing is ticked, so this drafts from every failure in the run — tick cases above to narrow it'
        : 'Every case is ticked, so this drafts from every failure in the run — untick some to narrow it',
  }
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

  // Graduated cases the last run got wrong — selectable for exactly the reasons promoted ones are.
  // Sharpening a promoted case routinely breaks something already in the corpus, and that
  // regression is the whole reason the "…with the eval corpus too" button exists. Offering the
  // batch alone made it visible and then unactionable: the improve request was pinned to the
  // promoted selection, so a corpus case could never be drafted from, and if the promoted case had
  // meanwhile started passing the step refused outright — telling the operator to "pick a case the
  // last run failed" while showing no such case to pick.
  const regressions = detail.cases.filter(isFailing)

  const [params, setParams] = useSearchParams()
  const ids = [...pending, ...regressions].map((c) => c.id)
  const selected = selectionFrom(params.get('cases'), ids)
  const batchRun = params.get('run')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [notice, setNotice] = useState('')
  const [instruction, setInstruction] = useState('')
  // The consolidating pass. Off by default and per-launch rather than a setting: an ordinary
  // improve is asked to fix named failures, and a list of rules nothing tests invites unrelated
  // deletion into the same diff.
  const [distill, setDistill] = useState(false)
  // Off by default: reusing an identical baseline is sound by construction, and the point of the
  // cache is not having to think about it. Ticked, this run measures the baseline again.
  const [freshBaseline, setFreshBaseline] = useState(false)

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

  const setSelected = (next: ReadonlySet<string>) => patch({ cases: selectionParam(next, ids) })

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
  const selectable = [...pending, ...regressions]
  const heldSelected = selectable.filter((c) => selected.has(c.id) && c.holdout)
  const targetable = selectable.filter((c) => selected.has(c.id) && !c.holdout).map((c) => c.id)
  // The same blindfold blocks the *improve* step, which is where it actually hurt: a lone promoted
  // case that hashes into the holdout could be scored, seen to miss, and then handed to a drafter
  // forbidden from looking at it — one wasted call and a draft that changed nothing.
  const noneDraftable = selected.size > 0 && heldSelected.length === selected.size

  // What a gate on a no-batch draft should be made to prove. Holdout cases are excluded: a change
  // may not claim to fix a case the improve loop was never shown, and the gate refuses such a claim.
  const failedLastRun = regressions.filter((c) => !c.holdout).map((c) => c.id)

  // The one request the sharpen step is described by, held here rather than written out at each
  // use: the launch button spends it and the prompt panel renders it, and those two disagreeing
  // about the run, the selection or the steer would make the preview a plausible fiction.
  // Narrowed only when the selection is a strict subset. Everything ticked (or nothing) means
  // "draft from whatever this run got wrong", which is the honest reading of an untouched
  // selection and the one that keeps a corpus regression in scope. Sending the full tick list
  // instead pinned the drafter to the promoted cases and silently dropped every other failure.
  const narrowed = narrowedCases(selected, ids)
  const improveRequest: JobRequest = {
    skill_id: skillId,
    run_id: hasBatch ? batchRun : latestRunId,
    ...(narrowed ? { cases: narrowed } : {}),
    instruction,
    ...(distill ? { distill: true } : {}),
  }

  const { label: sharpenLabel, scope: sharpenScope } = sharpenWording(
    narrowed,
    selected.size,
    hasBatch,
    distill,
  )

  // The score buttons run `scope: 'promoted'`, which accepts promoted ids and refuses any other,
  // so the selection is intersected rather than passed through — ticking a corpus regression must
  // not make the batch score fail to launch. Scoring the corpus is the second button's job.
  const promotedSelected = pending.filter((c) => selected.has(c.id)).map((c) => c.id)
  const scoreSubset = promotedSelected.length > 0 && promotedSelected.length < pending.length
  // `scoreKey` resets the launch button's cost plan whenever the selection changes.
  const scoreKey = scoreSubset ? [...promotedSelected].sort().join(',') : 'all'

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
      // Marked applied, not cleared. Clearing it swapped the whole review — the rationale, the
      // per-file diffs, the list of files touched — for a one-line notice, exactly when an
      // operator wants to check what they just wrote to disk.
      { onSuccess: () => setDraft((current) => current && { ...current, applied: true }) },
    )
  const onDrafted = (job: { result?: unknown }) => {
    const r = (job.result ?? {}) as Record<string, unknown>
    const body = String(r.body ?? '')
    const pages = (r.pages ?? {}) as Record<string, string>
    const claims = (r.sidecar_claims ?? []) as RoutedClaim[]
    const disputes = (r.disputed_claims ?? []) as DisputedClaim[]
    const rejected = (r.rejected_claims ?? []) as RejectedClaim[]
    // A draft with no guidance change is not necessarily an empty one. A run that routes every
    // lesson to a folder's notes is the *best* outcome this loop has — and it arrives with an empty
    // body, so bailing on the body alone threw the claims away and reported "no change".
    if (
      !body &&
      !Object.keys(pages).length &&
      !claims.length &&
      !disputes.length &&
      !rejected.length
    ) {
      setNotice('The drafter proposed no change.')
      return
    }
    setDraft({
      body,
      pages,
      rationale: String(r.rationale ?? ''),
      selectedMissing: (r.selected_missing ?? []) as string[],
      removedRules: (r.removed_rules ?? []) as RemovedRule[],
      claims,
      disputes,
      rejected: (r.rejected_claims ?? []) as RejectedClaim[],
      misrouted: (r.misrouted ?? []) as string[],
      namedSymbols: (r.named_symbols ?? []) as string[],
      duplicated: (r.duplicated ?? []) as string[],
      // Snapshotted here, once, while it still describes what the draft was written against.
      baseline: { body: proposal?.body ?? '', pages: proposal?.pages ?? {} },
    })
  }

  const review = draft && (
    <DraftReview
      draft={draft}
      applying={save.isPending}
      readOnly={readOnly}
      error={save.error}
      editorSearch={editorSearch}
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

      {(hasBatch || regressions.length > 0) && (
        <section className="rounded-lg border border-line bg-surface p-4">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-sm font-medium">
              {hasBatch ? 'Proposed cases' : 'Cases to sharpen'} ({selected.size} of {ids.length}{' '}
              selected)
            </h3>
            <div className="flex gap-3 text-xs text-muted">
              <button type="button" onClick={() => setSelected(new Set(ids))}>
                all
              </button>
              <button
                type="button"
                onClick={() => setSelected(new Set(selectable.filter(isFailing).map((c) => c.id)))}
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
            The checkboxes drive what gets scored, sharpened and gate-targeted. Tick a few to
            sharpen from just those; leave all (or none) ticked to draft from everything the last
            run got wrong. Caught / missed is from the latest score.
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
                {/* What the case actually asserts, which is the only field that tells one from
                    another. Adjudicating one review mints several cases from one file, so kind +
                    source + path renders them as identical rows — and this list is exactly where
                    the "sharpen against these" link lands. The id stays as the title, for the
                    times you need to match a row against a gate's output. */}
                <span className="min-w-0 flex-1 truncate" title={c.id}>
                  {c.semantic || <span className="font-mono text-xs text-muted">{c.id}</span>}
                </span>
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

          {/* The corpus cases this work broke. Listed here rather than left to the run drill-down
              because seeing a regression and being unable to act on it is the state that stalls the
              loop: sharpening a promoted case routinely breaks something already graduated, and
              that is precisely the change that must not be committed. */}
          {regressions.length > 0 && (
            <div className={hasBatch ? 'mt-4 border-t border-line pt-3' : ''}>
              {hasBatch && (
                <p className="mb-2 text-xs">
                  <span className="text-warn">
                    {regressions.length} graduated case(s) the last run got wrong
                  </span>{' '}
                  <span className="text-muted">
                    — tick them to sharpen from them too. A change that fixes the batch by breaking
                    the corpus is the one the gate exists to stop.
                  </span>
                </p>
              )}
              <ul className="space-y-1.5">
                {regressions.map((c) => (
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
                    <Link
                      to={`/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(c.id)}`}
                      className="font-mono text-xs text-muted hover:text-ink"
                    >
                      {c.id}
                    </Link>
                    {c.holdout && (
                      <Badge
                        tone="neutral"
                        title="Holdout — scored on every run, but the gate can't target it and the improve loop never learns from it"
                      >
                        holdout
                      </Badge>
                    )}
                    <span className="ml-auto">
                      <CaseStatus c={c} />
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
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
              regressions too — it costs every graduated case, which is why it is the second button
              rather than the only one.
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <LaunchButton
                key={scoreKey}
                kind="eval"
                request={{
                  skill_id: skillId,
                  scope: 'promoted',
                  ...(scoreSubset ? { cases: promotedSelected } : {}),
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
                  ...(scoreSubset ? { cases: promotedSelected } : {}),
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
          {/* Both paths, and both reachable. This used to offer hand-editing in prose and only
              the LLM in fact — the editor is on another tab and nothing here said so, let alone
              linked to it. */}
          {hasBatch ? (
            <p className="mt-0.5 mb-2 text-xs text-muted">
              Draft a change with the LLM from the cases you selected, or{' '}
              <Link to={{ search: editorSearch }} className="text-accent underline">
                edit the files by hand
              </Link>{' '}
              in the Edit tab (your ticked cases and this score come back with you). Either way it
              lands on disk and is re-scored above.
            </p>
          ) : (
            <p className="mt-0.5 mb-2 text-xs text-muted">
              No promoted batch waiting — sharpening runs against the corpus cases the last run got
              wrong. Draft with the LLM here, or{' '}
              <Link to={{ search: editorSearch }} className="text-accent underline">
                edit the files on disk by hand
              </Link>
              . To sharpen against fresh signal instead,{' '}
              <Link to="/triage" className="underline">
                promote cases in Triage
              </Link>
              .
            </p>
          )}

          {/* One launcher for both paths, because the selection belongs to both. `improveRequest`
              carries `cases` whether or not a promoted batch exists, so the no-batch path — the
              common one — was quietly sending a narrowed draft while its button said "Draft from
              the last run" and its own copy said "every case the last run failed", flatly
              contradicting the checkbox panel above it. It also had no `noneDraftable` guard, so
              an all-holdout selection got as far as a cost banner for a draft the server was
              always going to refuse. Duplicating the guard would have re-invited the drift; this
              leaves nowhere for the two to disagree. */}
          {hasBatch || latestRunId ? (
            <>
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
                  here to sharpen from. Cases still waiting under{' '}
                  <code className="font-mono">promoted_cases/</code> are always available — the exam
                  is the graduated corpus — so tick one of those, or a graduated case outside the
                  holdout. To spend a graduated holdout case anyway, record{' '}
                  <code className="font-mono">partition: train</code> in its case file; it then
                  counts as taught rather than as an unseen pass.
                </p>
              )}
              <LaunchButton
                kind="improve"
                request={improveRequest}
                label={sharpenLabel}
                disabled={noneDraftable}
                disabledReason="Every ticked case is holdout — the drafter is never shown one."
                onDone={onDrafted}
              >
                {/* An empty selection is not an empty draft: the server reads no ids as "no
                    filter" and learns from every failure in the run. Saying "drafts from the 0
                    selected case(s)" described the opposite of what the button does — and sat
                    directly above a cost banner correctly saying "up to N clustered failure(s)". */}
                <p className="mb-2 text-xs text-muted">
                  {sharpenScope}
                  {hasBatch && !batchRun
                    ? ' — score them first for the drafter to see what fails'
                    : ''}
                  .
                </p>
                <Steer value={instruction} onChange={setInstruction} />
                {/* Inside the confirmation, with the cost: it changes what the drafter is shown,
                    and the plan below restates how many rules that turns out to be. */}
                <label className="mt-2 flex cursor-pointer items-start gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={distill}
                    onChange={(e) => setDistill(e.target.checked)}
                    className="mt-0.5"
                  />
                  <span>
                    <span className="font-medium">Distill</span> — also show the drafter the rules
                    no eval case is linked to, so it can consolidate them.{' '}
                    <span className="text-muted">
                      The monthly pass the cadence clock asks for. Removing one of those fails
                      nothing, so the draft will name what it took out and you decide.
                    </span>
                  </span>
                </label>
              </LaunchButton>
              {/* Outside the LaunchButton, not inside its confirmation: the point of reading the
                  prompt is to decide whether to spend at all, and the confirmation is one click too
                  late for that. It tracks the same request, so ticking a case changes both. */}
              <ImprovePromptPanel request={improveRequest} />
            </>
          ) : (
            <p className="text-xs text-muted italic">
              Never scored — run evals from the header first, so the drafter can see what the
              guidance currently gets wrong.
            </p>
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
              key={`${(hasBatch ? targetable : failedLastRun).join(',')}|${freshBaseline}`}
              kind="gate"
              request={{
                skill_id: skillId,
                targeted: hasBatch ? targetable : failedLastRun,
                fresh_baseline: freshBaseline,
              }}
              label="Run the gate"
            />
            {/* The baseline is the last commit, so a gate minutes after another one measures
                content that did not move — it is reused, halving the spend and removing a second
                sample of a reviewer that may not answer the same way twice. This is the escape
                hatch for the one input the reuse key cannot see: the model behind a name changing
                under you. */}
            <label className="flex items-center gap-1.5 text-xs text-muted">
              <input
                type="checkbox"
                checked={freshBaseline}
                onChange={(e) => setFreshBaseline(e.target.checked)}
              />
              <span title="Off, an identical baseline already on record is reused instead of being measured again — same commit, cases, judge, reviewer and model. Tick this to measure it again anyway.">
                re-measure the baseline
              </span>
            </label>
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

/** A rule the draft takes out, and the cases that would notice — see `deadrules.removed_rules`. */
export type RemovedRule = { rule_id: string; linked_cases: string[] }

/**
 * A lesson the drafter sent to a folder's notes instead of the guidance.
 *
 * The one output of an improve that is deliberately *not* in the diff below it: a claim is
 * delivered as a patch the folder's owners accept in the repository that owns the file, and
 * Whetstone holds no write credentials there. So this panel is the whole of the handover — without
 * `patch` on the wire there is no way to get it out of the console at all, and an operator who read
 * only the diff would conclude the drafter had dropped the lesson.
 */
export type RoutedLesson = { claim: string; excepts: string; because: string }

export type RoutedClaim = {
  path: string
  folder: string
  /** Every lesson routed to this file. One patch per file, so two claims about one folder are a
   *  sequence rather than two rival versions of it — see `improve.SidecarPatch`. */
  claims: RoutedLesson[]
  patch: string
  creates_file: boolean
}

/** A proposed claim the checks refused, and why. Reported, never silently dropped. */
export type RejectedClaim = { folder: string; claim: string; reason: string }

/** A claim in the notes the drafter says the failures contradict. Filed to the ledger, not written. */
export type DisputedClaim = { path: string; claim: string; evidence?: string }

export type Draft = {
  body: string
  pages: Record<string, string>
  rationale: string
  selectedMissing: string[]
  removedRules: RemovedRule[]
  claims: RoutedClaim[]
  disputes: DisputedClaim[]
  rejected: RejectedClaim[]
  misrouted: string[]
  namedSymbols: string[]
  duplicated: string[]
  // The on-disk guidance as it stood when this draft arrived, captured rather than read live.
  //
  // Applying invalidates the skill, so the live `proposal` refetches to the *new* on-disk text —
  // and a diff computed against that shows no change at all. The review would go blank at the
  // moment it becomes the record of what was written. It also stops a background refetch altering
  // a diff someone is midway through reading.
  baseline: Baseline
  // Set once the write has landed. The panel stays and says so, rather than vanishing: what was
  // applied is the thing you most want to look at immediately after applying it.
  applied?: boolean
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

/** The on-disk guidance a draft would replace: `SKILL.md` plus every companion page, by path. */
export type Baseline = { body: string; pages: Record<string, string> }

/** One file the draft rewrites, ready to render as a diff. */
export type DraftedFile = { path: string; before: string; after: string }

/**
 * Every file a draft actually rewrites — `SKILL.md` when the body moved, plus each companion page
 * the drafter returned.
 *
 * A skill is a folder and the improve step edits it as one, so the review has to show it as one. A
 * panel that rendered only the body would silently drop a rule the drafter fixed in
 * `references/x.md`, and the operator would apply a change believing they had read all of it.
 *
 * Extracted from the component because that claim is worth testing and a component this repo has no
 * renderer for cannot be. `pages` arrives already filtered by the server: `GuidanceProposal.
 * changed_pages` drops anything handed back unchanged, so a page present here really did change.
 */
export function draftedFiles(
  draft: { body: string; pages: Record<string, string> },
  baseline: Baseline,
): DraftedFile[] {
  return [
    ...(draft.body.trim() !== baseline.body.trim()
      ? [{ path: 'SKILL.md', before: baseline.body, after: draft.body }]
      : []),
    // Sorted, so two drafts touching the same files review in the same order — a diff that moves
    // between renders is one people stop reading.
    ...Object.entries(draft.pages)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([path, after]) => ({ path, before: baseline.pages[path] ?? '', after })),
  ]
}

/**
 * What this draft produced, in one sentence — guidance files *and* claims.
 *
 * The count was files alone, and read as the whole of the output: a run that filed a claim and
 * rewrote `SKILL.md` said "a change to 1 file", so an operator who read the diff and stopped there
 * never learned the second half existed. It is not in the diff by design — a claim is a patch
 * against someone else's repository — which is exactly why the count has to say so.
 */
export function draftSummary(files: number, claims: number): string {
  const f = `${files} file${files === 1 ? '' : 's'}`
  const c = `${claims} claim${claims === 1 ? '' : 's'} beside the code`
  if (claims === 0) return `a change to ${f}`
  if (files === 0) return `${c} and no change to the guidance`
  return `a change to ${f}, and ${c}`
}

/** The `.patch` filename for a claim: the target path, flattened so it survives a download. */
export function patchFilename(path: string): string {
  return `${path.replace(/[\\/]/g, '_').replace(/\.md$/, '')}.patch`
}

/**
 * What this draft removes from the guidance, split by whether anything would notice.
 *
 * The split is the point. A rule with cases linked to it can be removed carelessly and the gate
 * will say so — that is the ordinary path and it needs no warning. A rule with nothing linked to
 * it is the one edit in the loop with no downstream check at all, so the only thing between it and
 * the guidance is whoever is reading this panel.
 */
function RemovedRules({ removed }: { removed: RemovedRule[] }) {
  if (removed.length === 0) return null
  const unbacked = removed.filter((r) => r.linked_cases.length === 0)
  const backed = removed.filter((r) => r.linked_cases.length > 0)
  return (
    <div className="space-y-1">
      {unbacked.length > 0 && (
        <p className="rounded border border-bad/50 bg-bad/5 px-2.5 py-2 text-xs text-bad">
          <strong>
            Removes {unbacked.map((r) => r.rule_id).join(', ')} — no case is linked to
            {unbacked.length === 1 ? ' it' : ' them'}.
          </strong>{' '}
          <span className="text-ink">
            Nothing downstream can check this: scoring, the gate and the merge all pass, because
            having no case linked to it is exactly what makes them pass. If the rule was doing work,
            this is where that is decided.
          </span>
        </p>
      )}
      {backed.map((rule) => (
        <p key={rule.rule_id} className="text-xs text-muted">
          Removes {rule.rule_id} — linked to {rule.linked_cases.join(', ')}, so the gate will judge
          it.
        </p>
      ))}
    </div>
  )
}

/** Copy to the clipboard, then say so for a moment. Falls back to a visible failure, not silence. */
function useCopy(): [string, (key: string, text: string) => void] {
  const [copied, setCopied] = useState('')
  const copy = (key: string, text: string) => {
    // The timer starts when the write *settles*, not when it is requested. Started alongside, a
    // slow clipboard clears the label before it is ever set and the button never says anything.
    const clear = () => window.setTimeout(() => setCopied(''), 2000)
    void navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(key)
        clear()
      })
      .catch(() => {
        setCopied('failed')
        clear()
      })
  }
  return [copied, copy]
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/x-patch' }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  // Revoked on the next tick, not on this one. The click is dispatched synchronously but the
  // fetch the browser starts for it is not, and revoking in the same task cancels the download on
  // enough browsers to be worth the timeout.
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * The half of a draft that is not in the diff, and what to do about each part of it.
 *
 * Three outputs land here, and all three used to be on the wire and rendered nowhere — the server
 * comments said *"Surfaced on the draft"* and *"Shown over the diff"* of fields no component read.
 * The job log carried them, which is a different screen and one scrolled past.
 *
 * Every block ends in something the reader can do. A claim is only useful if it can leave the
 * console, so it carries the patch itself; a duplicate is only a problem if you cannot pick a side,
 * so it says which two sides and links to the editor that resolves it. A panel that reported these
 * and stopped would be handing over a diagnosis and keeping the cure.
 */
export function NotInTheDiff({ draft, editorSearch }: { draft: Draft; editorSearch: string }) {
  const [copied, copy] = useCopy()
  // `misrouted` arrives with the duplicates already taken out — see `_log_routed`. Filtering here
  // as well would be a second spelling of `improve.same_place`, in a second language, free to
  // disagree with the first about which folder contains which.
  const { claims, disputes, rejected, duplicated, misrouted, namedSymbols } = draft
  if (
    !claims.length &&
    !disputes.length &&
    !rejected.length &&
    !duplicated.length &&
    !misrouted.length &&
    !namedSymbols.length
  )
    return null

  return (
    <div className="space-y-2">
      {duplicated.length > 0 && (
        <div className="rounded border border-bad/50 bg-bad/5 px-2.5 py-2 text-xs">
          <p className="text-bad">
            <strong>
              The same lesson is in both homes for {duplicated.map((f) => f).join(', ')}.
            </strong>{' '}
            <span className="text-ink">
              It was filed as a claim <em>and</em> written into the guidance. One home per lesson: a
              rule that has to name a folder to be correct is weaker everywhere it was already
              working.
            </span>
          </p>
          <p className="mt-1.5 text-muted">
            Keep it local — take the patch below and{' '}
            <Link to={{ search: editorSearch }} className="text-accent underline">
              delete the paragraph in the Edit tab
            </Link>{' '}
            before applying. Or keep it central: apply the diff and do not deliver the patch.
          </p>
        </div>
      )}
      {namedSymbols.length > 0 && (
        <p className="rounded border border-warn/50 bg-warn/5 px-2.5 py-2 text-xs text-warn">
          <strong>
            The new guidance pins a rule to {namedSymbols.join(', ')}; the old one did not.
          </strong>{' '}
          <span className="text-ink">
            A rule whose trigger is one class is a fact about that class, written in the file that
            applies everywhere. It belongs in the notes beside it — the folder its file lives in is
            listed in the prompt. Occasionally naming a type is right;{' '}
            <Link to={{ search: editorSearch }} className="text-accent underline">
              edit it out
            </Link>{' '}
            if it is not.
          </span>
        </p>
      )}
      {misrouted.length > 0 && (
        <p className="rounded border border-warn/50 bg-warn/5 px-2.5 py-2 text-xs text-warn">
          <strong>The new guidance names {misrouted.join(', ')}; the old one did not.</strong>{' '}
          <span className="text-ink">
            A fact about one folder belongs in that folder&rsquo;s notes, not in the file that
            applies everywhere. Occasionally naming a path is right —{' '}
            <Link to={{ search: editorSearch }} className="text-accent underline">
              edit it out
            </Link>{' '}
            if it is not.
          </span>
        </p>
      )}
      {claims.map((c) => (
        <div key={c.path} className="rounded border border-accent/40 bg-accent/5 px-2.5 py-2">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-mono text-xs text-accent">{c.path}</span>
            <span className="text-[11px] text-muted">
              {c.creates_file ? 'creates the file' : 'adds to the file'}
              {c.claims.length > 1 ? ` · ${c.claims.length} claims` : ''}
            </span>
          </div>
          {/* Every lesson routed to this file, in the order the one patch below adds them. */}
          {c.claims.map((lesson) => (
            <div key={lesson.claim} className="mt-1">
              <p className="text-xs text-ink">
                {lesson.excepts && (
                  <span className="mr-1.5 rounded bg-warn/15 px-1 text-[11px] text-warn">
                    Excepts {lesson.excepts}
                  </span>
                )}
                {lesson.claim}
              </p>
              {lesson.because && (
                <p className="mt-0.5 text-[11px] text-muted">Local because: {lesson.because}</p>
              )}
            </div>
          ))}
          <div className="mt-1.5 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => copy(c.path, c.patch)}
              className="rounded border border-line px-2 py-0.5 text-xs text-ink hover:bg-line/30"
            >
              {copied === c.path ? 'Copied' : 'Copy patch'}
            </button>
            <button
              type="button"
              onClick={() => download(patchFilename(c.path), c.patch)}
              className="rounded border border-line px-2 py-0.5 text-xs text-ink hover:bg-line/30"
            >
              Download .patch
            </button>
          </div>
        </div>
      ))}
      {claims.length > 0 && (
        <p className="text-[11px] text-muted">
          Nothing was written. A claim is delivered as a pull request in the repository that owns
          the file, in front of that folder&rsquo;s owners — the console holds no credentials there.
        </p>
      )}
      {rejected.map((r) => (
        <div key={`${r.folder}:${r.claim}`} className="rounded border border-line px-2.5 py-2">
          <p className="text-xs text-muted">
            <strong className="text-ink">Refused a claim for {r.folder || '(no folder)'}</strong> —{' '}
            {r.reason}
          </p>
          <p className="mt-1 text-[11px] text-muted">
            <q>{r.claim}</q>
          </p>
        </div>
      ))}
      {disputes.map((d) => (
        <div key={`${d.path}:${d.claim}`} className="rounded border border-warn/40 px-2.5 py-2">
          <p className="font-mono text-xs text-warn">{d.path}</p>
          <p className="mt-1 text-xs text-ink">
            Disputed: <q>{d.claim}</q>
          </p>
          {d.evidence && <p className="mt-0.5 text-[11px] text-muted">Because: {d.evidence}</p>}
          <p className="mt-0.5 text-[11px] text-muted">
            Filed to the ledger — a skill never rewrites the notes it reads. It shows on the Sidecar
            tab, and <code className="font-mono">whetstone sidecars claims --disputed</code> is the
            queue.
          </p>
        </div>
      ))}
      {copied === 'failed' && (
        <p className="text-xs text-bad">
          The clipboard refused. Use <strong>Download .patch</strong> instead.
        </p>
      )}
    </div>
  )
}

function DraftReview({
  draft,
  applying,
  readOnly,
  error,
  editorSearch,
  onApply,
  onDiscard,
}: {
  draft: Draft
  applying: boolean
  readOnly: boolean
  error: unknown
  editorSearch: string
  onApply: () => void
  onDiscard: () => void
}) {
  // Against the snapshot on the draft, never the live query — see `Draft.baseline`. Applying
  // refetches the on-disk guidance, and diffing against *that* would blank the review at the moment
  // it becomes the record of what was written.
  const files = draftedFiles(draft, draft.baseline)
  const applied = Boolean(draft.applied)
  return (
    <div
      className={`mt-3 space-y-2 rounded-lg border p-3 ${
        applied ? 'border-good/40 bg-good/5' : 'border-accent/40 bg-accent/5'
      }`}
    >
      <p className={`text-xs ${applied ? 'text-good' : 'text-muted'}`}>
        {applied ? (
          <>
            Applied to disk — {files.length} file{files.length === 1 ? '' : 's'} written. This is
            what was written; re-score to see whether it caught them.
            {draft.claims.length > 0 && (
              <>
                {' '}
                {draft.claims.length === 1
                  ? 'The claim below was not written — deliver it separately.'
                  : `The ${draft.claims.length} claims below were not written — deliver them separately.`}
              </>
            )}
          </>
        ) : (
          <>
            Drafted {draftSummary(files.length, draft.claims.length)}. Read it before applying — the
            drafter is not the reviewer.
          </>
        )}
      </p>
      {draft.rationale && <p className="text-sm">{draft.rationale}</p>}
      {/* Above the diff, not below it. A removed rule is one deleted paragraph among reworded
          ones, and the ones with nothing linked to them are the single edit in this whole loop
          that no later step can catch — scoring it, gating it and merging it all pass, because
          passing is what "no case is linked to it" means. */}
      <RemovedRules removed={draft.removedRules} />
      {/* Above the diff for the same reason: what is *not* in the diff is the thing a reader
          scrolling a diff will never find. The duplicate warning in particular has to be read
          before "Apply", not after. */}
      <NotInTheDiff draft={draft} editorSearch={editorSearch} />
      {files.length === 0 ? (
        <p className="text-xs text-warn">
          {draft.claims.length > 0
            ? 'No change to the guidance — every lesson went to a folder’s notes. That is a result, not an empty draft.'
            : 'The drafter returned no change to any file.'}
        </p>
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
          Not drafted from: {draft.selectedMissing.join(', ')} — the score did not fail them (or
          they are holdout), so they were not shown to the drafter.
        </p>
      )}
      <div className="flex gap-2">
        {/* Gone once it has landed, rather than left enabled: a second click would write the same
            content again and bump the version for nothing. */}
        {!applied && (
          <button
            type="button"
            onClick={onApply}
            disabled={readOnly || applying || files.length === 0}
            className="rounded border border-good/50 px-3 py-1 text-sm text-good hover:bg-good/10 disabled:opacity-40"
          >
            {applying ? 'Applying…' : 'Apply to disk'}
          </button>
        )}
        {/* "Discard" throws away work; "Close" puts away a record of work already done. Same
            handler, and the word is the only thing telling you which one you are about to do. */}
        <button type="button" onClick={onDiscard} className="px-2 py-1 text-sm text-muted">
          {applied ? 'Close' : 'Discard'}
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

export function isFailing(c: PendingCase): boolean {
  return c.kind === 'should_catch'
    ? c.last_recall != null && c.last_recall < 1
    : c.last_fp_rate != null && c.last_fp_rate > 0
}

function fmt(v: unknown): string {
  return typeof v === 'number' ? v.toFixed(2) : '—'
}
