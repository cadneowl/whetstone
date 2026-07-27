import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ApiError,
  useConsoleConfig,
  usePromoteFinding,
  usePromoteMissed,
  useReview,
  useRuleOnFinding,
  useUndoFindingVerdict,
  type Finding,
  type PromoteResponse,
  type ReviewRecord,
} from '@/api/client'
import { DiffView, type Overlay } from '@/components/diff/DiffView'
import { Badge, Empty, ErrorNote, Loading, severityName, when } from '@/components/primitives'

/** A committed eval case, as the card/panel remembers it after a successful promote. */
type Committed = { caseId: string; branch: string }

function apiMessage(error: unknown): string {
  return error instanceof ApiError ? error.problem.message : String(error)
}

/**
 * Ruling on what a skill said about a live change.
 *
 * Two panes: the findings, and the diff they are about. Selecting a finding highlights the lines it
 * cites, because "is this right?" is unanswerable without seeing the code it is pointing at — and a
 * reviewer citing the wrong line is itself one of the ways a finding is wrong.
 *
 * A ruling writes a candidate into the triage queue rather than an eval case directly. For a
 * rejected finding the case is complete as minted ("stay silent here"); for a confirmed one the
 * `semantic` is still the reviewer's own message, so triage is where a human rewrites it into
 * something that does not grade the reviewer against its own words.
 */
export function ReviewDetail() {
  const { reviewId = '' } = useParams()
  const { data, isLoading, error } = useReview(reviewId)
  const { data: config } = useConsoleConfig()
  const [selected, setSelected] = useState(0)

  const rule = useRuleOnFinding(reviewId)
  const undo = useUndoFindingVerdict(reviewId)
  const makeCase = usePromoteFinding(reviewId)

  const record = data?.record
  const overlays: Overlay[] = useMemo(() => {
    const finding = record?.findings[selected]
    if (!finding?.line) return []
    const verdict = record?.verdicts.find((v) => v.finding_index === selected)
    return [
      {
        range: [finding.line, finding.line],
        path: finding.path,
        kind: 'expectation',
        // Green once confirmed, amber once rejected: the overlay agrees with the ruling instead of
        // restating the reviewer's own confidence in itself.
        tone: verdict ? (verdict.correct ? 'accent' : 'warn') : 'accent',
      },
    ]
  }, [record, selected])

  const readOnly = Boolean(config?.read_only)
  const findings = record?.findings ?? []

  // Adjudicating a dozen findings is the same volume activity triage is, and had been mouse-only.
  useReviewKeys({
    enabled: !readOnly && findings.length > 0 && !rule.isPending && !undo.isPending,
    onNext: () => setSelected((i) => Math.min(findings.length - 1, i + 1)),
    onPrev: () => setSelected((i) => Math.max(0, i - 1)),
    onRule: (correct) => rule.mutate({ index: selected, correct }),
    onUndo: () => {
      if (record?.verdicts.some((v) => v.finding_index === selected)) undo.mutate(selected)
    },
  })

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data || !record) return <Empty>Not found.</Empty>

  const changeFiles = (record.change?.files ?? []).map((f) => f.path)

  return (
    <div>
      <Header record={record} stale={data.stale_skill} />

      {record.findings.length === 0 ? (
        <>
          <Empty>
            The skill found nothing here. That is a result too — but it is not evidence yet: nothing
            on this screen can tell whether it stayed quiet correctly or missed something.
          </Empty>
          <MissedCasePanel
            reviewId={reviewId}
            skillId={record.skill_id}
            files={changeFiles}
            readOnly={readOnly}
            defaultOpen
          />
        </>
      ) : (
        <>
          <div className="grid gap-4 xl:h-[calc(100vh-17rem)] xl:min-h-[28rem] xl:grid-cols-[26rem_minmax(0,1fr)] xl:grid-rows-[minmax(0,1fr)] 2xl:grid-cols-[32rem_minmax(0,1fr)]">
            <div className="min-w-0 space-y-2 xl:h-full xl:min-h-0 xl:overflow-y-auto xl:pr-1">
              {record.findings.map((finding, i) => (
                <FindingCard
                  key={i}
                  finding={finding}
                  index={i}
                  selected={i === selected}
                  verdict={record.verdicts.find((v) => v.finding_index === i) ?? null}
                  readOnly={readOnly}
                  busy={rule.isPending || undo.isPending}
                  skillId={record.skill_id}
                  onSelect={() => setSelected(i)}
                  onRule={(correct, note) => rule.mutate({ index: i, correct, note })}
                  onUndo={() => undo.mutate(i)}
                  onMakeCase={(semantic) => makeCase.mutateAsync({ index: i, semantic })}
                />
              ))}
              {(rule.error || undo.error) && <ErrorNote error={rule.error ?? undo.error} />}
              <p className="pt-1 text-[11px] leading-relaxed text-muted">
                <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>c</kbd> correct · <kbd>f</kbd> false positive
                · <kbd>u</kbd> undo
              </p>
            </div>

            <div className="min-w-0 xl:h-full xl:min-h-0 xl:overflow-y-auto">
              <DiffView diff={data.diff} selection={null} overlays={overlays} />
            </div>
          </div>
          <MissedCasePanel
            reviewId={reviewId}
            skillId={record.skill_id}
            files={changeFiles}
            readOnly={readOnly}
          />
        </>
      )}
    </div>
  )
}

/** Keyboard adjudication. Mirrors triage's `j`/`k`, with `c`/`f` for the verdict and `u` to undo. */
function useReviewKeys({
  enabled,
  onNext,
  onPrev,
  onRule,
  onUndo,
}: {
  enabled: boolean
  onNext: () => void
  onPrev: () => void
  onRule: (correct: boolean) => void
  onUndo: () => void
}) {
  useEffect(() => {
    if (!enabled) return
    function handle(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null
      // Never steal keystrokes from the note field — `f` is a letter people type.
      if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return
      if (event.metaKey || event.ctrlKey || event.altKey) return

      const actions: Record<string, () => void> = {
        j: onNext,
        k: onPrev,
        c: () => onRule(true),
        f: () => onRule(false),
        u: onUndo,
      }
      const action = actions[event.key]
      if (action) {
        event.preventDefault()
        action()
      }
    }
    window.addEventListener('keydown', handle)
    return () => window.removeEventListener('keydown', handle)
  }, [enabled, onNext, onPrev, onRule, onUndo])
}

function Header({ record, stale }: { record: ReviewRecord; stale: boolean }) {
  return (
    <header className="mb-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="min-w-0 truncate text-lg font-semibold">{record.title || record.ref}</h1>
        {record.url ? (
          <a
            href={record.url}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-xs text-muted underline decoration-dotted hover:text-accent"
          >
            {record.ref}
          </a>
        ) : (
          <span className="font-mono text-xs text-muted">{record.ref}</span>
        )}
        <span className="text-sm text-muted">
          {record.confirmed} confirmed · {record.rejected} false · {record.pending} to rule
        </span>
        <span className="ml-auto text-xs text-muted">{when(record.created_at)}</span>
      </div>
      <p className="mt-1 flex flex-wrap items-center gap-2 font-mono text-xs text-muted">
        <Link to={`/skills/${encodeURIComponent(record.skill_id)}`} className="hover:text-accent">
          {record.skill_id}
        </Link>
        <span>v{record.skill_version}</span>
        {record.model && <span>{record.model}</span>}
        {record.head_ref && (
          <span title="the commit these findings are about">@{record.head_ref.slice(0, 8)}</span>
        )}
        {record.skill_hash_assumed && (
          <Badge
            tone="neutral"
            title="Whoever ran this review did not say which guidance produced it, so the version on disk was assumed. 'Not stale' is therefore an assumption, not a fact."
          >
            version assumed
          </Badge>
        )}
      </p>
      {stale && (
        <p className="mt-2 rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-sm text-warn">
          The guidance has been edited since this ran. These findings describe a reviewer that no
          longer exists, so a ruling here teaches the corpus about a version nobody runs — re-run
          the review before spending attention on it.
        </p>
      )}
    </header>
  )
}

function FindingCard({
  finding,
  index,
  selected,
  verdict,
  readOnly,
  busy,
  skillId,
  onSelect,
  onRule,
  onUndo,
  onMakeCase,
}: {
  finding: Finding
  index: number
  selected: boolean
  verdict: { correct: boolean; note: string; candidate_id: string } | null
  readOnly: boolean
  busy: boolean
  skillId: string
  onSelect: () => void
  onRule: (correct: boolean, note: string) => void
  onUndo: () => void
  onMakeCase: (semantic?: string) => Promise<PromoteResponse>
}) {
  const [note, setNote] = useState('')
  // Reopened on an already-ruled finding, so a typo in the note can be corrected. The note becomes
  // the expectation on a confirmed finding, which makes a typo in it a typo in the ground truth —
  // and the only alternative was undo (which deletes the candidate) followed by re-ruling.
  const [editing, setEditing] = useState(false)
  const composing = !verdict || editing

  // Committing the case straight from here, skipping the trip through triage. A rejection or a
  // confirmation-with-note goes in one click; a bare confirmation comes back asking for a
  // description, because the expectation cannot be the reviewer's own message.
  const [committed, setCommitted] = useState<Committed | null>(null)
  const [making, setMaking] = useState(false)
  const [makeError, setMakeError] = useState('')
  const [needsDescription, setNeedsDescription] = useState(false)
  const [description, setDescription] = useState('')

  async function make(semantic?: string) {
    setMaking(true)
    setMakeError('')
    try {
      const res = await onMakeCase(semantic)
      setCommitted({ caseId: res.prepared.case_id, branch: res.branch })
      setNeedsDescription(false)
    } catch (error) {
      const message = apiMessage(error)
      if (/standalone description/.test(message)) {
        setNeedsDescription(true)
        setDescription((current) => current || verdict?.note || '')
      } else {
        setMakeError(message)
      }
    } finally {
      setMaking(false)
    }
  }

  return (
    <section
      onClick={onSelect}
      className={`min-w-0 cursor-pointer rounded-lg border bg-surface p-3 transition-colors ${
        selected ? 'border-accent' : 'border-line hover:border-accent/40'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted">#{index + 1}</span>
        {finding.rule_id && <Badge tone="accent">{finding.rule_id}</Badge>}
        <Badge tone={finding.severity >= 30 ? 'bad' : finding.severity >= 20 ? 'warn' : 'neutral'}>
          {severityName(finding.severity)}
        </Badge>
        {verdict && (
          <Badge tone={verdict.correct ? 'good' : 'warn'}>
            {verdict.correct ? 'confirmed' : 'false positive'}
          </Badge>
        )}
      </div>

      <p className="mt-1.5 min-w-0 font-mono text-[11px] break-all text-muted">
        {finding.path}
        {finding.line ? `:${finding.line}` : ''}
      </p>
      <p className="mt-1 text-sm break-words">{finding.message}</p>

      {verdict && !editing && (
        <div className="mt-2 space-y-1.5 text-xs text-muted">
          {verdict.note && <p className="break-words italic">“{verdict.note}”</p>}

          {committed ? (
            <p className="text-good">
              ✓ Committed as{' '}
              <Link
                to={`/skills/${encodeURIComponent(skillId)}`}
                className="font-mono underline decoration-dotted hover:text-accent"
                onClick={(e) => e.stopPropagation()}
              >
                {committed.caseId}
              </Link>{' '}
              on <span className="font-mono">{committed.branch}</span>. Score the batch on the skill
              page, then gate it.
            </p>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                {!readOnly && (
                  <button
                    type="button"
                    disabled={busy || making}
                    onClick={(e) => {
                      e.stopPropagation()
                      void make()
                    }}
                    className="rounded-lg border border-good/50 px-2.5 py-1 font-medium text-good transition-colors hover:bg-good/10 disabled:opacity-40"
                  >
                    {making ? 'committing…' : 'Make test case'}
                  </button>
                )}
                <span>
                  a{' '}
                  <span className="font-mono">
                    {verdict.correct ? 'should catch' : 'should not flag'}
                  </span>{' '}
                  case — or refine it in{' '}
                  <Link
                    to="/triage"
                    className="underline decoration-dotted hover:text-accent"
                    onClick={(e) => e.stopPropagation()}
                  >
                    triage
                  </Link>
                  .
                </span>
                {!readOnly && (
                  <>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={(e) => {
                        e.stopPropagation()
                        setNote(verdict.note)
                        setEditing(true)
                      }}
                      className="underline hover:text-ink disabled:opacity-40"
                    >
                      {verdict.note ? 'edit note' : 'add a note'}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={(e) => {
                        e.stopPropagation()
                        onUndo()
                      }}
                      className="underline hover:text-ink disabled:opacity-40"
                    >
                      undo
                    </button>
                  </>
                )}
              </div>

              {needsDescription && !readOnly && (
                <div className="mt-1 space-y-1.5" onClick={(e) => e.stopPropagation()}>
                  <p className="text-warn">
                    The expectation can’t be the reviewer’s own message — a case asserting the skill
                    says what it already said can never fail. Describe the problem in your own
                    words:
                  </p>
                  <textarea
                    value={description}
                    rows={2}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="e.g. the DB row can be missing on a normal path and must be handled"
                    className="w-full rounded border border-line bg-canvas px-2 py-1.5 text-xs"
                  />
                  <button
                    type="button"
                    disabled={making || !description.trim()}
                    onClick={() => void make(description.trim())}
                    className="rounded-lg border border-good/50 px-2.5 py-1 font-medium text-good transition-colors hover:bg-good/10 disabled:opacity-40"
                  >
                    Commit as test case
                  </button>
                </div>
              )}
              {makeError && <p className="text-bad">{makeError}</p>}
            </>
          )}
        </div>
      )}

      {composing && !readOnly && (
        <div className="mt-2.5 space-y-2">
          {/* Optional, and worth more than it looks. On a *correct* finding this text becomes the
              expectation, which is what stops the case grading the reviewer against its own words;
              on a false positive it is the reason, which is what the next person reading the case
              needs. */}
          <textarea
            value={note}
            rows={2}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Why? Optional — on a correct finding this becomes the expectation."
            className="w-full rounded border border-line bg-canvas px-2 py-1.5 text-xs"
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={(e) => {
                e.stopPropagation()
                setEditing(false)
                onRule(true, note)
              }}
              className="rounded-lg border border-good/50 px-3 py-1 text-sm text-good transition-colors hover:bg-good/10 disabled:opacity-40"
            >
              Correct
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={(e) => {
                e.stopPropagation()
                setEditing(false)
                onRule(false, note)
              }}
              className="rounded-lg border border-warn/50 px-3 py-1 text-sm text-warn transition-colors hover:bg-warn/10 disabled:opacity-40"
            >
              False positive
            </button>
            {editing && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  setEditing(false)
                }}
                className="px-2 py-1 text-sm text-muted hover:text-ink"
              >
                Cancel
              </button>
            )}
          </div>
        </div>
      )}
    </section>
  )
}

/**
 * Teach the skill about something it missed.
 *
 * The review screen can only rule on findings the skill *produced*; a false negative — the skill
 * staying silent where it should have spoken — has no finding to thumb-down. This is that path: pick
 * the file (and optionally the lines), describe what should have been caught, and it commits a
 * `should_catch` case straight onto the batch branch, exactly as promoting a confirmed finding does.
 */
function MissedCasePanel({
  reviewId,
  skillId,
  files,
  readOnly,
  defaultOpen = false,
}: {
  reviewId: string
  skillId: string
  files: string[]
  readOnly: boolean
  defaultOpen?: boolean
}) {
  const missed = usePromoteMissed(reviewId)
  const [open, setOpen] = useState(defaultOpen)
  const [path, setPath] = useState(files[0] ?? '')
  const [lineStart, setLineStart] = useState('')
  const [lineEnd, setLineEnd] = useState('')
  const [semantic, setSemantic] = useState('')
  const [ruleId, setRuleId] = useState('')
  const [severity, setSeverity] = useState('')
  const [committed, setCommitted] = useState<Committed | null>(null)
  const [error, setError] = useState('')

  if (readOnly) return null

  async function submit() {
    setError('')
    try {
      const res = await missed.mutateAsync({
        skill_id: skillId,
        path,
        semantic: semantic.trim(),
        line_start: lineStart ? Number(lineStart) : null,
        line_end: lineEnd ? Number(lineEnd) : null,
        rule_id: ruleId.trim(),
        severity_min: severity || null,
      })
      setCommitted({ caseId: res.prepared.case_id, branch: res.branch })
      setSemantic('')
      setLineStart('')
      setLineEnd('')
      setRuleId('')
    } catch (e) {
      setError(apiMessage(e))
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-4 text-sm text-muted underline decoration-dotted hover:text-accent"
      >
        + The skill missed something? Add a case it should catch
      </button>
    )
  }

  const input = 'mt-1 rounded border border-line bg-canvas px-2 py-1.5 text-xs'

  return (
    <section className="mt-4 rounded-lg border border-line bg-surface p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-semibold">Add a case the skill missed</h2>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="text-xs text-muted hover:text-ink"
        >
          Close
        </button>
      </div>
      <p className="mt-1 text-xs text-muted">
        The skill stayed silent where it should have spoken. This commits a{' '}
        <span className="font-mono">should catch</span> case straight to the batch — there is no
        finding to rule on, so your description is the ground truth it will be judged against.
      </p>

      {committed ? (
        <p className="mt-3 text-sm text-good">
          ✓ Committed as{' '}
          <Link
            to={`/skills/${encodeURIComponent(skillId)}`}
            className="font-mono underline decoration-dotted hover:text-accent"
          >
            {committed.caseId}
          </Link>{' '}
          on <span className="font-mono">{committed.branch}</span>.{' '}
          <button
            type="button"
            className="underline hover:text-ink"
            onClick={() => setCommitted(null)}
          >
            add another
          </button>
        </p>
      ) : (
        <div className="mt-3 space-y-3">
          <label className="block text-xs">
            <span className="text-muted">File</span>
            <select
              value={path}
              onChange={(e) => setPath(e.target.value)}
              className={`${input} w-full font-mono`}
            >
              {files.length === 0 && <option value="">(the change touches no files)</option>}
              {files.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </label>
          <div className="flex flex-wrap gap-3">
            <label className="block text-xs">
              <span className="text-muted">First line</span>
              <input
                value={lineStart}
                onChange={(e) => setLineStart(e.target.value.replace(/\D/g, ''))}
                inputMode="numeric"
                placeholder="whole file"
                className={`${input} block w-28`}
              />
            </label>
            <label className="block text-xs">
              <span className="text-muted">Last line</span>
              <input
                value={lineEnd}
                onChange={(e) => setLineEnd(e.target.value.replace(/\D/g, ''))}
                inputMode="numeric"
                placeholder="= first"
                className={`${input} block w-28`}
              />
            </label>
            <label className="block text-xs">
              <span className="text-muted">Min severity</span>
              <select
                value={severity}
                onChange={(e) => setSeverity(e.target.value)}
                className={`${input} block`}
              >
                <option value="">any</option>
                <option value="info">info</option>
                <option value="warning">warning</option>
                <option value="error">error</option>
              </select>
            </label>
            <label className="block text-xs">
              <span className="text-muted">Rule</span>
              <input
                value={ruleId}
                onChange={(e) => setRuleId(e.target.value)}
                placeholder="R1"
                className={`${input} block w-24`}
              />
            </label>
          </div>
          <label className="block text-xs">
            <span className="text-muted">What the skill should have said</span>
            <textarea
              value={semantic}
              onChange={(e) => setSemantic(e.target.value)}
              rows={2}
              placeholder="Describe the problem the reviewer missed — the expectation this case is judged against."
              className={`${input} block w-full`}
            />
          </label>
          {error && <p className="text-xs text-bad">{error}</p>}
          <button
            type="button"
            disabled={missed.isPending || !path || !semantic.trim()}
            onClick={() => void submit()}
            className="rounded-lg border border-good/50 px-3 py-1 text-sm text-good transition-colors hover:bg-good/10 disabled:opacity-40"
          >
            {missed.isPending ? 'committing…' : 'Commit as test case'}
          </button>
        </div>
      )}
    </section>
  )
}
