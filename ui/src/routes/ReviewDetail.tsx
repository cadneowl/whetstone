import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  useConsoleConfig,
  useReview,
  useRuleOnFinding,
  useUndoFindingVerdict,
  type Finding,
  type ReviewRecord,
} from '@/api/client'
import { DiffView, type Overlay } from '@/components/diff/DiffView'
import { Badge, Empty, ErrorNote, Loading, severityName, when } from '@/components/primitives'

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

  return (
    <div>
      <Header record={record} stale={data.stale_skill} />

      {record.findings.length === 0 ? (
        <Empty>
          The skill found nothing here. That is a result too — but it is not evidence yet: nothing
          on this screen can tell whether it stayed quiet correctly or missed something.
        </Empty>
      ) : (
        <div className="grid gap-4 xl:h-[calc(100vh-14rem)] xl:min-h-[28rem] xl:grid-cols-[26rem_minmax(0,1fr)] xl:grid-rows-[minmax(0,1fr)] 2xl:grid-cols-[32rem_minmax(0,1fr)]">
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
                onSelect={() => setSelected(i)}
                onRule={(correct, note) => rule.mutate({ index: i, correct, note })}
                onUndo={() => undo.mutate(i)}
              />
            ))}
            {(rule.error || undo.error) && <ErrorNote error={rule.error ?? undo.error} />}
            <p className="pt-1 text-[11px] leading-relaxed text-muted">
              <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>c</kbd> correct · <kbd>f</kbd> false positive ·{' '}
              <kbd>u</kbd> undo
            </p>
          </div>

          <div className="min-w-0 xl:h-full xl:min-h-0 xl:overflow-y-auto">
            <DiffView diff={data.diff} selection={null} overlays={overlays} />
          </div>
        </div>
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
        {record.head_ref && <span title="the commit these findings are about">
          @{record.head_ref.slice(0, 8)}
        </span>}
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
  onSelect,
  onRule,
  onUndo,
}: {
  finding: Finding
  index: number
  selected: boolean
  verdict: { correct: boolean; note: string; candidate_id: string } | null
  readOnly: boolean
  busy: boolean
  onSelect: () => void
  onRule: (correct: boolean, note: string) => void
  onUndo: () => void
}) {
  const [note, setNote] = useState('')
  // Reopened on an already-ruled finding, so a typo in the note can be corrected. The note becomes
  // the expectation on a confirmed finding, which makes a typo in it a typo in the ground truth —
  // and the only alternative was undo (which deletes the candidate) followed by re-ruling.
  const [editing, setEditing] = useState(false)
  const composing = !verdict || editing

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
        <div className="mt-2 space-y-1 text-xs text-muted">
          {verdict.note && <p className="break-words italic">“{verdict.note}”</p>}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span>
              Queued as{' '}
              <Link
                to="/triage"
                className="font-mono underline decoration-dotted hover:text-accent"
                onClick={(e) => e.stopPropagation()}
              >
                {verdict.correct ? 'should catch' : 'should not flag'}
              </Link>{' '}
              in triage.
              {verdict.correct &&
                (verdict.note
                  ? ' Your explanation became the expectation.'
                  : ' Rewrite the semantic there — as minted it repeats this message.')}
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
