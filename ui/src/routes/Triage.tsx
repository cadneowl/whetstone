import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  useBatch,
  useConsoleConfig,
  useDraftPlan,
  useDraftSemantic,
  usePreview,
  usePromote,
  usePropose,
  useQueue,
  useReject,
  useSkills,
  type CaseEdits,
  type EvalKind,
  type QueueItem,
} from '@/api/client'
import { DiffView, type Overlay, type Selection } from '@/components/diff/DiffView'
import { LaunchButton } from '@/components/LaunchButton'
import { DiscussionPane } from '@/components/DiscussionPane'
import { Badge, Empty, ErrorNote, Intro, Loading, severityName } from '@/components/primitives'
import { SIGNALS, SignalBadge, signalMeta } from '@/components/signals'

/**
 * The triage queue.
 *
 * Three panes, left to right: what is queued, the evidence, and what will be recorded. The middle
 * one leads with the review conversation rather than the diff, because the diff alone is just a
 * code change — the reason it is a candidate at all is what somebody said about it.
 *
 * `corpus/builder.py` sets a candidate's expectation to the raw body of the first review comment —
 * "nit: use ? here", "see above", "👍" — and that text becomes the ground truth the judge scores
 * every finding against. So the thread and the editable field are both on screen: the job is to
 * *rewrite* the signal, not to accept it. The region is dragged on the diff rather than typed,
 * because an auto-generated line range is the field most likely to be wrong.
 */
// Shared by the populated screen and the empty one, so the two cannot describe the job differently.
const TRIAGE_INTRO = (
  <>
    Signal mined from merge requests, waiting to become eval cases. For each one: check the evidence
    in the middle, fix the fields on the right — <em>rewrite the semantic</em>, it arrives as the
    raw review comment — then Promote. Promoted cases land on a batch branch: score the skill
    against them to see what it misses before you propose them as one MR. Reject anything the miner
    guessed wrong.
  </>
)

export function Triage() {
  const { data: queue, isLoading, error } = useQueue()
  const { data: batch } = useBatch()
  // A batch can hold cases for more than one skill. Offer the score button only when it is
  // unambiguous; with several, the Skills page is the place to pick one.
  const skillOnBatch = batch?.skills?.length === 1 ? batch.skills[0] : null
  const { data: config } = useConsoleConfig()
  const { data: skills } = useSkills()

  const [rawIndex, setIndex] = useState(0)
  const [edits, setEdits] = useState<CaseEdits | null>(null)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  // Text, plus an optional link the forge offered — only a push has one.
  const [notice, setNotice] = useState<{ text: string; url?: string | null } | null>(null)
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set())

  const all = useMemo(() => queue?.items ?? [], [queue])
  const items = useMemo(() => all.filter((i) => !hidden.has(signalOf(i))), [all, hidden])
  const index = Math.min(rawIndex, Math.max(0, items.length - 1))
  const current: QueueItem | undefined = items[index]

  // `?focus=<candidate-id>` opens the queue on a specific candidate — how the health panel's
  // uncovered-MRs list lands here. Consumed once the queue has loaded, so moving through the
  // queue afterwards is not pinned back to it, and a candidate already ruled on is simply absent.
  const [params, setParams] = useSearchParams()
  const focus = params.get('focus')
  useEffect(() => {
    if (!focus || items.length === 0) return
    const at = items.findIndex((i) => i.entry.candidate.id === focus)
    if (at >= 0) setIndex(at)
    setParams(
      (p) => {
        p.delete('focus')
        return p
      },
      { replace: true },
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, items.length])

  const preview = usePreview()
  const promote = usePromote()
  const reject = useReject()
  const propose = usePropose()

  // Reset the form whenever the selected candidate changes — keyed on its *content*, not its id.
  //
  // A candidate minted from a review ruling keeps a stable id when the ruling is changed, so an
  // id-keyed reset left the kind and semantic showing the previous ruling while the "As generated"
  // box beside them showed the new one. Promoting then wrote the ruling that had been withdrawn.
  // Serializing is cheap and a no-op refetch produces an identical string, so nothing resets under
  // someone mid-edit unless the candidate really did change.
  const editsKey = current ? JSON.stringify(current.edits) : ''
  useEffect(() => {
    setEdits(current ? { ...current.edits } : null)
    setRejecting(false)
    setReason('')
    preview.reset()
    promote.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editsKey])

  const move = useCallback(
    (delta: number) => setIndex((i) => Math.max(0, Math.min(items.length - 1, i + delta))),
    [items.length],
  )

  /**
   * Any edit invalidates the last validation result. Leaving the green "Valid — would commit …"
   * banner up after the form changed would vouch for something that is no longer what would be
   * committed.
   */
  const applyEdits = useCallback(
    (next: CaseEdits) => {
      setEdits(next)
      preview.reset()
    },
    [preview],
  )

  const promoteWith = useCallback(
    (tier: 'active' | 'archive') => {
      if (!current || !edits) return
      promote.mutate(
        { id: current.entry.candidate.id, edits: { ...edits, tier } },
        {
          onSuccess: (result) => {
            setNotice({
              text:
                `${result.prepared.case_id} → ${result.branch} (${result.batch_commits} queued)` +
                (tier === 'archive' ? ' — archived: counted at low weight' : ''),
            })
            setIndex((i) => Math.min(i, Math.max(0, items.length - 2)))
          },
        },
      )
    },
    [current, edits, items.length, promote],
  )
  const doPromote = useCallback(() => promoteWith('active'), [promoteWith])
  const doArchive = useCallback(() => promoteWith('archive'), [promoteWith])

  const doReject = useCallback(() => {
    if (!current || !reason.trim()) return
    reject.mutate(
      { id: current.entry.candidate.id, reason },
      {
        onSuccess: () => {
          setNotice({ text: `rejected ${current.entry.candidate.id}` })
          setRejecting(false)
          setReason('')
        },
      },
    )
  }, [current, reason, reject])

  useKeyboard({
    enabled: !rejecting && Boolean(current) && !config?.read_only,
    onNext: () => move(1),
    onPrev: () => move(-1),
    onPromote: doPromote,
    onReject: () => setRejecting(true),
  })

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!queue?.available) {
    // Titled and explained even when empty: this is the screen a first-time operator reaches
    // before anything has been mined, so a bare error is the worst possible first impression of it.
    return (
      <div>
        <header className="mb-4">
          <h1 className="text-lg font-semibold">Triage</h1>
          <Intro>{TRIAGE_INTRO}</Intro>
        </header>
        <Empty>
          Nothing mined yet — no candidate directory at{' '}
          <code className="font-mono">{queue?.root}</code>. Turn on{' '}
          <code className="font-mono">[watch]</code> in whetstone.toml, or run{' '}
          <code className="font-mono">
            whetstone corpus pull --out {queue?.root ?? 'candidates'}
          </code>
          .
        </Empty>
      </div>
    )
  }

  return (
    <div>
      <header className="mb-4">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="text-lg font-semibold">Triage</h1>
          <span className="text-sm text-muted">
            {queue.counts.pending} pending · {queue.counts.promoted} promoted ·{' '}
            {queue.counts.rejected} rejected
          </span>
          {batch && (
            <span className="ml-auto flex items-center gap-3 text-sm">
              <span className="font-mono text-xs text-muted">{batch.branch}</span>
              {/* Promoting writes cases to this branch and never to the working tree, so until
                  this existed the cases just curated were invisible to every way of running the
                  skill — the only route to "does the reviewer catch these?" was to merge the merge
                  request and find out afterwards. Which is backwards: testing against a case is
                  the reason to promote it. */}
              {batch.commits > 0 && skillOnBatch && (
                <LaunchButton
                  kind="eval"
                  request={{ skill_id: skillOnBatch, scope: 'batch' }}
                  label="Score these cases"
                />
              )}
              {batch.commits > 0 && (
                <button
                  type="button"
                  onClick={() =>
                    propose.mutate(batch.branch, {
                      onSuccess: (r) => setNotice({ text: r.message, url: r.merge_request_url }),
                    })
                  }
                  disabled={config?.read_only || propose.isPending}
                  className="rounded-lg border border-accent/50 px-3 py-1 text-accent transition-colors hover:bg-accent/10 disabled:opacity-40"
                >
                  Propose {batch.commits} case{batch.commits === 1 ? '' : 's'}
                </button>
              )}
            </span>
          )}
        </div>
        <Intro>{TRIAGE_INTRO}</Intro>
      </header>

      <SignalFilter items={all} hidden={hidden} onToggle={setHidden} />

      {notice && (
        <p className="mb-3 rounded-lg border border-good/40 bg-good/5 px-3 py-2 text-sm">
          {notice.text}
          {/* The forge hands back the address of its own "open a merge request" page when a new
              branch is pushed. Offering it here is the difference between finishing the job and
              being told to go and find the page yourself. */}
          {notice.url && (
            <>
              {' '}
              <a
                href={notice.url}
                target="_blank"
                rel="noreferrer"
                className="text-accent underline"
              >
                Open the merge request →
              </a>
            </>
          )}
        </p>
      )}
      {propose.error && <ErrorNote error={propose.error} />}

      {all.length === 0 ? (
        <Empty>Queue is clear — every candidate has been decided.</Empty>
      ) : items.length === 0 ? (
        <Empty>
          Every one of the {all.length} pending candidates is hidden by the filter above.{' '}
          <button
            type="button"
            onClick={() => setHidden(new Set())}
            className="underline hover:text-ink"
          >
            Show all
          </button>
        </Empty>
      ) : (
        // `minmax(0, …)` on the middle track, not `1fr`: a grid track sizes to its content's
        // intrinsic minimum by default, so one long unbroken path in a diff would widen the column
        // and push the form off the screen.
        // One viewport tall, with each pane scrolling itself. Stacked-and-scrolling wasted the
        // bottom half of a wide screen on nothing while pushing the promote button off the
        // bottom — and a hundred queued candidates must not be able to shove the diff out of view.
        // `grid-rows-[minmax(0,1fr)]` as well as a height: a grid row is auto-sized by default and
        // grows past its container, so without it the panes size to their content and `h-full`
        // measures a row that is already 2000px tall. Both tracks need the same `minmax(0, …)`.
        <div className="grid gap-4 xl:h-[calc(100vh-13rem)] xl:min-h-[28rem] xl:grid-cols-[15rem_minmax(0,1fr)_22rem] xl:grid-rows-[minmax(0,1fr)] 2xl:grid-cols-[17rem_minmax(0,1fr)_26rem]">
          <QueuePane items={items} index={index} onPick={setIndex} />

          <div className="flex min-w-0 flex-col gap-4 xl:h-full xl:min-h-0">
            {/* Capped rather than fixed: a short thread gives its space to the diff, a long one
                scrolls instead of burying it. */}
            {current && (
              <div className="shrink-0 xl:max-h-[45%] xl:overflow-y-auto">
                <DiscussionPane candidate={current.entry.candidate} />
              </div>
            )}
            <div className="min-h-0 flex-1 xl:overflow-y-auto">
              {current && edits && (
                <DiffView
                  diff={current.entry.diff}
                  selection={
                    edits.line_range
                      ? { path: edits.path, range: edits.line_range as [number, number] }
                      : null
                  }
                  onSelect={(s: Selection) =>
                    applyEdits({
                      ...edits,
                      path: s?.path ?? edits.path,
                      line_range: s?.range ?? null,
                    })
                  }
                  overlays={overlaysFor(edits)}
                />
              )}
            </div>
          </div>

          {current && edits && (
            <FormPane
              item={current}
              edits={edits}
              onChange={applyEdits}
              skillIds={(skills ?? []).map((s) => s.id)}
              readOnly={Boolean(config?.read_only)}
              rejecting={rejecting}
              reason={reason}
              onReason={setReason}
              onStartReject={() => setRejecting(true)}
              onCancelReject={() => setRejecting(false)}
              onReject={doReject}
              onPromote={doPromote}
              onArchive={doArchive}
              onValidate={() => preview.mutate({ id: current.entry.candidate.id, edits })}
              busy={promote.isPending || reject.isPending}
              error={promote.error ?? preview.error ?? reject.error}
              validated={preview.data ?? null}
            />
          )}
        </div>
      )}
    </div>
  )
}

function signalOf(item: QueueItem): string {
  return item.entry.candidate.provenance.human_signal ?? ''
}

/**
 * Filter the queue by what each candidate is evidence *of*.
 *
 * A comment-free merge yields one `merged clean` candidate per changed file, so a repo that
 * reviews by talking rather than by commenting inline produces a queue that is mostly those —
 * and they are the weakest thing the builder makes. Sorted strongest-first they sit at the
 * bottom, but "scroll until it gets boring" is not a filter. This is.
 *
 * Doubles as the legend: every chip carries the signal's meaning on hover.
 */
function SignalFilter({
  items,
  hidden,
  onToggle,
}: {
  items: QueueItem[]
  hidden: ReadonlySet<string>
  onToggle: (next: ReadonlySet<string>) => void
}) {
  const counts = useMemo(() => {
    const out = new Map<string, number>()
    for (const item of items) out.set(signalOf(item), (out.get(signalOf(item)) ?? 0) + 1)
    return out
  }, [items])

  // Builder order for the ones we know, then anything unrecognized (hand-written, or a signal
  // added since this shipped) so a chip never silently disappears from the queue's account of itself.
  const present = [
    ...SIGNALS.map((s) => s.id).filter((id) => counts.has(id)),
    ...[...counts.keys()].filter((id) => !SIGNALS.some((s) => s.id === id)),
  ]
  if (present.length < 2) return null

  return (
    <div className="mb-4 flex flex-wrap items-center gap-1.5">
      {present.map((id) => {
        const off = hidden.has(id)
        const meta = signalMeta(id)
        return (
          <button
            key={id}
            type="button"
            title={meta.meaning}
            aria-pressed={!off}
            onClick={() => {
              const next = new Set(hidden)
              if (off) next.delete(id)
              else next.add(id)
              onToggle(next)
            }}
            className={`rounded-full border px-2.5 py-0.5 text-xs transition-colors ${
              off
                ? 'border-line text-muted line-through opacity-50 hover:opacity-80'
                : 'border-line hover:border-accent/50'
            }`}
          >
            {meta.id} <span className="tabular text-muted">{counts.get(id)}</span>
          </button>
        )
      })}
      {hidden.size > 0 && (
        <button
          type="button"
          onClick={() => onToggle(new Set())}
          className="px-1.5 text-xs text-muted underline hover:text-ink"
        >
          show all
        </button>
      )}
    </div>
  )
}

type Severity = NonNullable<CaseEdits['severity_min']>
const SEVERITIES: Severity[] = [10, 20, 30]

function overlaysFor(edits: CaseEdits): Overlay[] {
  if (!edits.line_range) return []
  return [
    {
      range: edits.line_range as [number, number],
      path: edits.path,
      kind: 'expectation',
      tone: edits.kind === 'should_catch' ? 'accent' : 'warn',
    },
  ]
}

/**
 * Draft the expectation, in two clicks like every other spend.
 *
 * Rewriting the mined comment into a standalone description of the problem is the one genuinely
 * irreducible step in triage, and the one that stops being possible at a hundred thousand
 * promotions. The draft is never adopted: it lands in the field beside it for a person to accept,
 * edit or throw away, and `semantic_drafted_by` records which model wrote it so the two populations
 * stay tellable apart.
 */
function DraftButton({
  candidateId,
  skillId,
  disabled,
  onDrafted,
}: {
  candidateId: string
  skillId: string
  disabled?: boolean
  onDrafted: (semantic: string, by: string) => void
}) {
  const plan = useDraftPlan()
  const draft = useDraftSemantic()

  if (draft.data?.draft.rationale && !plan.data) {
    return (
      <span className="ml-auto text-[11px] normal-case" title={draft.data.draft.rationale}>
        <button
          type="button"
          onClick={() => plan.mutate({ id: candidateId, skillId })}
          className="text-muted transition-colors hover:text-accent"
        >
          redraft
        </button>
      </span>
    )
  }

  if (plan.data) {
    return (
      <span className="ml-auto flex items-center gap-2 normal-case">
        <span className="text-[11px] text-warn" title={plan.data.estimate?.basis}>
          {plan.data.backend} · 1 call
        </span>
        <button
          type="button"
          disabled={draft.isPending}
          onClick={() =>
            draft.mutate(
              { id: candidateId, skillId },
              {
                onSuccess: (r) => {
                  onDrafted(r.draft.semantic, r.drafted_by)
                  plan.reset()
                },
              },
            )
          }
          className="rounded border border-accent/50 px-1.5 py-0.5 text-[11px] text-accent hover:bg-accent/10"
        >
          {draft.isPending ? 'Drafting…' : 'Yes, draft it'}
        </button>
        <button
          type="button"
          onClick={() => plan.reset()}
          className="text-[11px] text-muted hover:text-ink"
        >
          no
        </button>
      </span>
    )
  }

  return (
    <span className="ml-auto normal-case">
      <button
        type="button"
        disabled={disabled || plan.isPending || !skillId}
        title={
          skillId
            ? 'Rewrite this from the review evidence. The drafter is not shown the guidance.'
            : 'Choose a target skill first — the triage step lives in the skill folder.'
        }
        onClick={() => plan.mutate({ id: candidateId, skillId })}
        className="text-[11px] text-muted transition-colors hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
      >
        {plan.isPending ? 'checking…' : 'draft it'}
      </button>
      {(plan.error || draft.error) && (
        <span className="ml-2 text-[11px] text-bad">
          {String((plan.error ?? draft.error) as Error)}
        </span>
      )}
    </span>
  )
}

function QueuePane({
  items,
  index,
  onPick,
}: {
  items: QueueItem[]
  index: number
  onPick: (i: number) => void
}) {
  return (
    <aside className="flex min-w-0 flex-col gap-3 xl:h-full xl:min-h-0">
      <ul className="min-h-0 flex-1 space-y-1 xl:overflow-y-auto">
        {items.map((item, i) => {
          const c = item.entry.candidate
          const comments = c.discussion?.comments?.length ?? 0
          return (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => onPick(i)}
                className={`w-full min-w-0 rounded-lg border px-2.5 py-2 text-left text-sm transition-colors ${
                  i === index
                    ? 'border-accent bg-accent/10'
                    : 'border-line bg-surface hover:border-accent/40'
                }`}
              >
                {/* Signal first. The id is a slug nobody reads and the confidence is a number
                    whose meaning *is* the signal, so leading with those made every row look
                    alike — which is how a queue of weak candidates passes for a queue. */}
                <div className="flex items-center gap-1.5">
                  <SignalBadge id={c.provenance.human_signal} short />
                  <span className="ml-auto tabular text-xs text-muted" title="builder confidence">
                    {c.confidence.toFixed(2)}
                  </span>
                </div>
                <div className="mt-1 flex items-baseline gap-2">
                  {/* `min-w-0` + `truncate`: without it a long project slug in the id widens the
                      button past its track and the confidence escapes the pane. */}
                  <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted">
                    {c.id}
                  </span>
                  {comments > 0 && (
                    <span
                      className="shrink-0 text-[11px] text-muted"
                      title="comments in the thread"
                    >
                      💬 {comments}
                    </span>
                  )}
                </div>
                <div className="truncate text-xs text-muted">{c.suggested_skill ?? 'unrouted'}</div>
              </button>
            </li>
          )
        })}
      </ul>
      {/* Pinned below the scrolling list rather than after it, so the shortcuts stay readable at
          the hundredth candidate as well as the first. */}
      <p className="shrink-0 text-[11px] leading-relaxed text-muted">
        <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>a</kbd> promote · <kbd>x</kbd> reject ·{' '}
        <kbd>Enter</kbd> promote
      </p>
    </aside>
  )
}

function FormPane({
  item,
  edits,
  onChange,
  skillIds,
  readOnly,
  rejecting,
  reason,
  onReason,
  onStartReject,
  onCancelReject,
  onReject,
  onPromote,
  onArchive,
  onValidate,
  busy,
  error,
  validated,
}: {
  item: QueueItem
  edits: CaseEdits
  onChange: (e: CaseEdits) => void
  skillIds: string[]
  readOnly: boolean
  rejecting: boolean
  reason: string
  onReason: (r: string) => void
  onStartReject: () => void
  onCancelReject: () => void
  onReject: () => void
  onPromote: () => void
  onArchive: () => void
  onValidate: () => void
  busy: boolean
  error: unknown
  validated: { case_id: string } | null
}) {
  const candidate = item.entry.candidate
  const rawComment = item.edits.semantic
  const rewritten = edits.semantic !== rawComment
  // A confirmed ruling seeds the expectation from the skill's own finding — the one case where
  // leaving it unedited is not merely weak but circular, and the server refuses it.
  const mustRewrite =
    !rewritten && edits.kind === 'should_catch' && candidate.provenance.source === 'skill_review'
  const range = edits.line_range
  const inverted = range != null && range[0] > range[1]

  return (
    // Signal, source and rationale live in the discussion pane now, beside the evidence for them.
    // This column is only what will be written.
    <aside className="flex min-w-0 flex-col text-sm xl:h-full xl:min-h-0">
      <div className="min-h-0 flex-1 space-y-4 xl:overflow-y-auto xl:pr-1">
        <Similars item={item} current={edits.semantic} />

        <Field label="Kind">
          <div className="flex gap-3">
            {(['should_catch', 'should_not_flag'] as EvalKind[]).map((kind) => (
              <label key={kind} className="flex items-center gap-1.5">
                <input
                  type="radio"
                  checked={edits.kind === kind}
                  onChange={() => onChange({ ...edits, kind })}
                  disabled={readOnly}
                />
                <span>{kind === 'should_catch' ? 'should catch' : 'should not flag'}</span>
              </label>
            ))}
          </div>
        </Field>

        <Field label="Target skill">
          <select
            value={edits.skill_id}
            onChange={(e) => onChange({ ...edits, skill_id: e.target.value })}
            disabled={readOnly}
            className="w-full rounded border border-line bg-canvas px-2 py-1"
          >
            <option value="">— choose a skill —</option>
            {skillIds.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Case id">
          <input
            value={edits.case_id}
            onChange={(e) => onChange({ ...edits, case_id: e.target.value })}
            disabled={readOnly}
            className="w-full rounded border border-line bg-canvas px-2 py-1 font-mono text-xs"
          />
        </Field>

        <Field label="Evidence for rule">
          {/* Optional. Set it and the source MR is filed under that rule in the skill's meta.yaml,
            in the same commit — the only record of why a piece of guidance exists, and the thing
            nobody remembers to update in a follow-up. */}
          <input
            value={edits.rule_id}
            onChange={(e) => onChange({ ...edits, rule_id: e.target.value.toUpperCase() })}
            disabled={readOnly}
            placeholder="R1 — optional"
            className="w-full rounded border border-line bg-canvas px-2 py-1 font-mono text-xs"
          />
          <p className="mt-1 text-xs text-muted">
            {edits.rule_id
              ? `Cites ${candidate.provenance.ref ?? 'this MR'} as evidence for ${edits.rule_id}.`
              : 'Leave empty if this case tests the skill without justifying one rule.'}
          </p>
        </Field>

        <Field label="Region">
          <p className="mb-1 font-mono text-xs break-all">{edits.path}</p>
          {/* Typed entry as well as dragging: dragging is faster, but it is mouse-only and imprecise
            at the edges of a hunk, and this is the field most likely to be wrong. */}
          <div className="flex items-center gap-1.5">
            <LineInput
              value={edits.line_range?.[0] ?? null}
              disabled={readOnly}
              label="first line"
              onChange={(v) => onChange({ ...edits, line_range: withLine(edits.line_range, 0, v) })}
            />
            <span className="text-muted">–</span>
            <LineInput
              value={edits.line_range?.[1] ?? null}
              disabled={readOnly}
              label="last line"
              onChange={(v) => onChange({ ...edits, line_range: withLine(edits.line_range, 1, v) })}
            />
            {edits.line_range ? (
              <button
                type="button"
                onClick={() => onChange({ ...edits, line_range: null })}
                disabled={readOnly}
                className="ml-1 text-xs text-muted hover:text-ink"
              >
                whole file
              </button>
            ) : (
              <span className="ml-1 text-xs text-muted">whole file</span>
            )}
          </div>
          {inverted && (
            <p className="mt-1 text-xs text-bad">
              first line is after the last — this region can never match
            </p>
          )}
        </Field>

        <Field label="Severity floor">
          <select
            value={edits.severity_min ?? ''}
            onChange={(e) =>
              onChange({
                ...edits,
                // Severity is a closed IntEnum on the wire; keep the union rather than widening it.
                severity_min: e.target.value ? (Number(e.target.value) as Severity) : null,
              })
            }
            disabled={readOnly}
            className="w-full rounded border border-line bg-canvas px-2 py-1"
          >
            <option value="">none</option>
            {SEVERITIES.map((value) => (
              <option key={value} value={value}>
                {severityName(value)}
              </option>
            ))}
          </select>
        </Field>

        <div>
          {/* What the builder generated, kept beside the field it seeded so "unedited" is checkable
            rather than asserted. For an escaped defect this is the tracker summary, which appears
            nowhere else on the screen. */}
          <p className="mb-1 text-[11px] tracking-wide text-muted uppercase">As generated</p>
          <blockquote className="max-h-24 overflow-y-auto rounded border border-line bg-surface px-2 py-1.5 text-xs break-words text-muted italic">
            {rawComment || <span className="not-italic">(none)</span>}
          </blockquote>
        </div>

        <div>
          <p className="mb-1 flex items-center gap-2 text-[11px] tracking-wide text-muted uppercase">
            Semantic
            {edits.semantic_drafted_by ? (
              <Badge
                tone="accent"
                title={`Drafted by ${edits.semantic_drafted_by} from the evidence, not from the guidance. Read it before promoting.`}
              >
                drafted
              </Badge>
            ) : (
              !rewritten && (
                <Badge
                  tone="warn"
                  title="This is still the raw review comment — the judge scores findings against it"
                >
                  unedited
                </Badge>
              )
            )}
            {/* Keyed on the candidate so its draft/plan mutation state is dropped when the queue
                moves on. Without this the button kept the previous candidate's result — showing
                "redraft" and the old rationale beside a fresh, unedited semantic. */}
            <DraftButton
              key={candidate.id}
              candidateId={candidate.id}
              skillId={edits.skill_id}
              disabled={readOnly}
              onDrafted={(semantic, by) =>
                onChange({ ...edits, semantic, semantic_drafted_by: by })
              }
            />
          </p>
          {/* Explained in place, because this is the field with the least obvious name and the
            largest consequence: it is the sentence every future run of this case is scored
            against, and a weak one fails silently forever — nothing downstream ever goes red to
            tell you. The other hints on this form are static; this one has to follow `kind`,
            because should_not_flag inverts what a good sentence describes, and an operator who
            writes the defect there has built a case asserting the opposite of what they meant. */}
          <p className="mb-1.5 text-xs text-muted">
            {edits.kind === 'should_catch' ? (
              <>
                What every finding is judged against: a standalone description of the problem here —
                not the reviewer's words, not the fix.
              </>
            ) : (
              <>
                What every finding is judged against: a standalone description of what is{' '}
                <em>correct</em> here, so a reviewer that complains can be recognised as wrong.
              </>
            )}{' '}
            Someone who never saw this merge request has to be able to check it.
          </p>
          <textarea
            value={edits.semantic}
            onChange={(e) => onChange({ ...edits, semantic: e.target.value })}
            disabled={readOnly}
            rows={4}
            className="w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm"
            placeholder="Describe the issue as the judge should understand it…"
          />
          {/* Said here, beside the field, rather than only as a rejection after the click. The
            server refuses this case either way; being told why while you can still fix it is the
            difference between a guard rail and a wall. */}
          {mustRewrite && (
            <p className="mt-1 text-xs text-warn">
              This is the skill's own finding, word for word. A case asserting that the reviewer
              says what it already said can never fail — rewrite it as a standalone description of
              the problem.
            </p>
          )}
          {!edits.semantic.trim() && (
            <p className="mt-1 text-xs text-warn">
              Required: this is the ground truth every finding is judged against.
            </p>
          )}
        </div>
      </div>

      {/* Pinned below the scrolling fields. Triage is a volume activity and the form is long
          enough to push the verdict off the bottom; having to scroll to say yes is how a queue
          stops getting worked. */}
      <div className="shrink-0 space-y-3 pt-3">
        {error != null && <ErrorNote error={error} />}
        {validated && !error && (
          <p className="rounded border border-good/40 bg-good/5 px-2 py-1.5 text-xs text-good">
            Valid — would commit {validated.case_id}
          </p>
        )}

        {rejecting ? (
          <div className="space-y-2 rounded-lg border border-bad/40 p-3">
            <label className="block text-xs text-muted">
              Why is this not a good eval case? Rejections tune the builder's confidence.
            </label>
            <textarea
              value={reason}
              onChange={(e) => onReason(e.target.value)}
              rows={2}
              autoFocus
              className="w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm"
            />
            <div className="flex gap-2">
              <button
                type="button"
                onClick={onReject}
                disabled={!reason.trim() || busy}
                className="rounded border border-bad/50 px-3 py-1 text-bad hover:bg-bad/10 disabled:opacity-40"
              >
                Reject
              </button>
              <button type="button" onClick={onCancelReject} className="px-2 py-1 text-muted">
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onPromote}
              disabled={readOnly || busy}
              className="rounded-lg border border-good/50 px-3 py-1.5 text-good transition-colors hover:bg-good/10 disabled:opacity-40"
            >
              Promote
            </button>
            {/* The middle disposition, offered only when there is a resemblance to act on:
                counted as regression insurance at low draw weight, rather than either
                re-verifying a duplicate forever or throwing real evidence away. */}
            {(item.similar_cases ?? []).length > 0 && (
              <button
                type="button"
                onClick={onArchive}
                disabled={readOnly || busy}
                title="Promote with tier: archive — kept as regression insurance, sampled at low weight"
                className="rounded-lg border border-line px-3 py-1.5 text-muted transition-colors hover:border-good/50 hover:text-good disabled:opacity-40"
              >
                Promote to archive
              </button>
            )}
            <button
              type="button"
              onClick={onValidate}
              disabled={readOnly || busy}
              className="rounded-lg border border-line px-3 py-1.5 text-muted transition-colors hover:text-ink disabled:opacity-40"
            >
              Validate
            </button>
            <button
              type="button"
              onClick={onStartReject}
              disabled={readOnly || busy}
              className="rounded-lg border border-line px-3 py-1.5 text-muted transition-colors hover:text-bad disabled:opacity-40"
            >
              Reject
            </button>
          </div>
        )}
      </div>
    </aside>
  )
}

/**
 * Existing cases this candidate may duplicate, expandable to the side-by-side expectations.
 *
 * Evidence, never a verdict: hundreds of MRs of real defects are heavily repetitive, and promoted
 * naively they skew the sample toward the skill's favourite catch — but the ninth unwrap case in a
 * new subsystem may be exactly the promotion you want. The chip surfaces the resemblance; the
 * dispositions below (promote / promote to archive / reject) stay a human's call.
 */
function Similars({ item, current }: { item: QueueItem; current: string }) {
  const similars = item.similar_cases ?? []
  if (similars.length === 0) return null
  const skillId = item.edits.skill_id
  return (
    <details className="rounded-lg border border-warn/40 bg-warn/5 px-3 py-2">
      <summary className="cursor-pointer text-xs text-warn">
        similar to {similars.length} existing case{similars.length === 1 ? '' : 's'} — consider
        “Promote to archive” or reject as a duplicate
      </summary>
      <ul className="mt-2 space-y-2">
        {similars.map((s) => (
          <li key={s.case_id} className="text-xs">
            <Link
              to={`/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(s.case_id)}`}
              className="font-mono hover:text-accent"
            >
              {s.case_id}
            </Link>
            <span className="ml-2 text-muted">{s.why}</span>
            {s.semantic && (
              <div className="mt-1 grid gap-1 sm:grid-cols-2">
                <p className="rounded border border-line bg-canvas px-2 py-1 text-muted">
                  <span className="mb-0.5 block text-[10px] uppercase">existing</span>
                  {s.semantic}
                </p>
                <p className="rounded border border-line bg-canvas px-2 py-1">
                  <span className="mb-0.5 block text-[10px] uppercase text-muted">
                    this candidate
                  </span>
                  {current || <em className="text-muted">no expectation yet</em>}
                </p>
              </div>
            )}
          </li>
        ))}
      </ul>
    </details>
  )
}

function LineInput({
  value,
  label,
  disabled,
  onChange,
}: {
  value: number | null
  label: string
  disabled: boolean
  onChange: (value: number | null) => void
}) {
  return (
    <input
      type="number"
      min={1}
      inputMode="numeric"
      aria-label={label}
      value={value ?? ''}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
      className="w-16 rounded border border-line bg-canvas px-1.5 py-1 text-right font-mono text-xs"
    />
  )
}

/** Editing one end of the range; clearing either end means "whole file". */
function withLine(
  current: [number, number] | null | undefined,
  index: 0 | 1,
  value: number | null,
): [number, number] | null {
  if (value === null) return null
  const base: [number, number] = current ? [current[0], current[1]] : [value, value]
  base[index] = value
  return base
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-1 text-[11px] tracking-wide text-muted uppercase">{label}</p>
      {children}
    </div>
  )
}

/** Queue navigation without reaching for the mouse — triage is a volume activity. */
function useKeyboard({
  enabled,
  onNext,
  onPrev,
  onPromote,
  onReject,
}: {
  enabled: boolean
  onNext: () => void
  onPrev: () => void
  onPromote: () => void
  onReject: () => void
}) {
  useEffect(() => {
    if (!enabled) return
    function handle(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null
      // Never steal keystrokes from the fields the person is typing into.
      if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
      if (event.metaKey || event.ctrlKey || event.altKey) return

      const actions: Record<string, () => void> = {
        j: onNext,
        k: onPrev,
        a: onPromote,
        x: onReject,
        Enter: onPromote,
      }
      const action = actions[event.key]
      if (action) {
        event.preventDefault()
        action()
      }
    }
    window.addEventListener('keydown', handle)
    return () => window.removeEventListener('keydown', handle)
  }, [enabled, onNext, onPrev, onPromote, onReject])
}
