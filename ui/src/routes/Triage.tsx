import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  useBatch,
  useConsoleConfig,
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
import { Badge, Empty, ErrorNote, Loading, severityName } from '@/components/primitives'

/**
 * The triage queue.
 *
 * `corpus/builder.py` sets a candidate's expectation to the raw body of the first review comment —
 * "nit: use ? here", "see above", "👍" — and that text becomes the ground truth the judge scores
 * every finding against. So the raw comment and the editable field are shown side by side, both
 * visible: the job is to *rewrite* the signal, not to accept it. The region is dragged on the diff
 * rather than typed, because an auto-generated line range is the field most likely to be wrong.
 */
export function Triage() {
  const { data: queue, isLoading, error } = useQueue()
  const { data: batch } = useBatch()
  const { data: config } = useConsoleConfig()
  const { data: skills } = useSkills()

  const [index, setIndex] = useState(0)
  const [edits, setEdits] = useState<CaseEdits | null>(null)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const [notice, setNotice] = useState<string | null>(null)

  const items = useMemo(() => queue?.items ?? [], [queue])
  const current: QueueItem | undefined = items[Math.min(index, items.length - 1)]

  const preview = usePreview()
  const promote = usePromote()
  const reject = useReject()
  const propose = usePropose()

  // Reset the form whenever the selected candidate changes.
  useEffect(() => {
    setEdits(current ? { ...current.edits } : null)
    setRejecting(false)
    setReason('')
    preview.reset()
    promote.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.entry.candidate.id])

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

  const doPromote = useCallback(() => {
    if (!current || !edits) return
    promote.mutate(
      { id: current.entry.candidate.id, edits },
      {
        onSuccess: (result) => {
          setNotice(`${result.prepared.case_id} → ${result.branch} (${result.batch_commits} queued)`)
          setIndex((i) => Math.min(i, Math.max(0, items.length - 2)))
        },
      },
    )
  }, [current, edits, items.length, promote])

  const doReject = useCallback(() => {
    if (!current || !reason.trim()) return
    reject.mutate(
      { id: current.entry.candidate.id, reason },
      {
        onSuccess: () => {
          setNotice(`rejected ${current.entry.candidate.id}`)
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
    return (
      <Empty>
        No candidate directory at <code className="font-mono">{queue?.root}</code>. Run{' '}
        <code className="font-mono">whetstone corpus pull --out {queue?.root ?? 'candidates'}</code>{' '}
        to fill the queue.
      </Empty>
    )
  }

  return (
    <div>
      <header className="mb-4 flex flex-wrap items-baseline gap-3">
        <h1 className="text-lg font-semibold">Triage</h1>
        <span className="text-sm text-muted">
          {queue.counts.pending} pending · {queue.counts.promoted} promoted ·{' '}
          {queue.counts.rejected} rejected
        </span>
        {batch && (
          <span className="ml-auto flex items-center gap-3 text-sm">
            <span className="font-mono text-xs text-muted">{batch.branch}</span>
            {batch.commits > 0 && (
              <button
                type="button"
                onClick={() =>
                  propose.mutate(batch.branch, {
                    onSuccess: (r) => setNotice(r.message),
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
      </header>

      {notice && (
        <p className="mb-3 rounded-lg border border-good/40 bg-good/5 px-3 py-2 text-sm">{notice}</p>
      )}
      {propose.error && <ErrorNote error={propose.error} />}

      {items.length === 0 ? (
        <Empty>Queue is clear — every candidate has been decided.</Empty>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[15rem_1fr_21rem]">
          <QueuePane items={items} index={index} onPick={setIndex} />

          <div>
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
              onValidate={() =>
                preview.mutate({ id: current.entry.candidate.id, edits })
              }
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
    <aside>
      <ul className="space-y-1">
        {items.map((item, i) => {
          const c = item.entry.candidate
          return (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => onPick(i)}
                className={`w-full rounded-lg border px-2.5 py-2 text-left text-sm transition-colors ${
                  i === index
                    ? 'border-accent bg-accent/10'
                    : 'border-line bg-surface hover:border-accent/40'
                }`}
              >
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-xs">{c.id}</span>
                  <span
                    className="ml-auto tabular text-xs text-muted"
                    title="corpus builder confidence"
                  >
                    {c.confidence.toFixed(2)}
                  </span>
                </div>
                <div className="mt-0.5 truncate text-xs text-muted">
                  {c.suggested_skill ?? 'unrouted'}
                </div>
              </button>
            </li>
          )
        })}
      </ul>
      <p className="mt-3 text-[11px] leading-relaxed text-muted">
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
  onValidate: () => void
  busy: boolean
  error: unknown
  validated: { case_id: string } | null
}) {
  const candidate = item.entry.candidate
  const rawComment = item.edits.semantic
  const rewritten = edits.semantic !== rawComment
  const range = edits.line_range
  const inverted = range != null && range[0] > range[1]

  return (
    <aside className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="accent">{candidate.confidence.toFixed(2)} confidence</Badge>
        {candidate.provenance.ref && (
          <span className="font-mono text-xs text-muted">{candidate.provenance.ref}</span>
        )}
      </div>
      {candidate.rationale && <p className="text-xs text-muted">{candidate.rationale}</p>}

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
        <p className="mb-1 text-[11px] tracking-wide text-muted uppercase">Original comment</p>
        <blockquote className="rounded border border-line bg-surface px-2 py-1.5 text-xs text-muted italic">
          {rawComment || <span className="not-italic">(none)</span>}
        </blockquote>
      </div>

      <div>
        <p className="mb-1 flex items-center gap-2 text-[11px] tracking-wide text-muted uppercase">
          Semantic
          {!rewritten && (
            <Badge tone="warn" title="This is still the raw review comment — the judge scores findings against it">
              unedited
            </Badge>
          )}
        </p>
        <textarea
          value={edits.semantic}
          onChange={(e) => onChange({ ...edits, semantic: e.target.value })}
          disabled={readOnly}
          rows={4}
          className="w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm"
          placeholder="Describe the issue as the judge should understand it…"
        />
      </div>

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
    </aside>
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
