import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  useCancelJob,
  useJob,
  useLaunchJob,
  usePlanJob,
  type Job,
  type JobKind,
  type JobRequest,
  type Plan,
} from '@/api/client'
import { Badge, ErrorNote } from '@/components/primitives'

/**
 * The one way the console starts work that spends money.
 *
 * Two clicks, never one. The first fetches the plan and shows it — the resolved backend, whether it
 * bills, and an upper bound on the calls; the second launches. That is the same contract the CLI
 * has (`--yes` skips its confirmation, nothing skips it by accident), and it comes from the same
 * `preflight` code, so the two cannot drift into disagreeing about what a run costs.
 *
 * Kept as one component rather than three because the sequence — plan, confirm, watch, report — is
 * identical for every job kind, and the differences are entirely in how the result reads.
 */
export function LaunchButton({
  kind,
  request,
  label,
  disabled,
  disabledReason,
  onDone,
  children,
}: {
  kind: JobKind
  request: JobRequest
  label: string
  disabled?: boolean
  disabledReason?: string
  /** Called once when a job finishes successfully — for a caller that wants to use the result. */
  onDone?: (job: Job) => void
  /** Rendered inside the confirmation, above the buttons: extra options for this launch. */
  children?: ReactNode
}) {
  const [jobId, setJobId] = useState<string | null>(null)
  const [seen, setSeen] = useState<string | null>(null)
  const plan = usePlanJob(kind)
  const launch = useLaunchJob(kind)
  const cancel = useCancelJob()
  const { data: job } = useJob(jobId)

  // `onDone` fires once per job. The polling query re-renders on every tick, so without this the
  // callback would run several times for one result.
  if (job && job.state === 'done' && job.id !== seen) {
    setSeen(job.id)
    onDone?.(job)
  }

  const running = job?.state === 'running'
  if (running || (job && jobId)) {
    return (
      <JobStatus
        job={job!}
        onCancel={() => cancel.mutate(job!.id)}
        onDismiss={() => {
          setJobId(null)
          plan.reset()
          launch.reset()
        }}
      />
    )
  }

  if (plan.data) {
    return (
      <div className="rounded-lg border border-warn/40 bg-warn/5 p-3">
        <PlanBanner plan={plan.data} />
        {children && <div className="mt-3">{children}</div>}
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={launch.isPending}
            onClick={() => launch.mutate(request, { onSuccess: (started) => setJobId(started.id) })}
            className="rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:text-muted"
          >
            {launch.isPending ? 'Starting…' : `Yes, ${label.toLowerCase()}`}
          </button>
          <button
            type="button"
            onClick={() => plan.reset()}
            className="text-sm text-muted transition-colors hover:text-ink"
          >
            Cancel
          </button>
        </div>
        {launch.error && (
          <div className="mt-3">
            <ErrorNote error={launch.error} />
          </div>
        )}
      </div>
    )
  }

  return (
    <div>
      <button
        type="button"
        disabled={disabled || plan.isPending}
        title={disabled ? disabledReason : undefined}
        onClick={() => plan.mutate(request)}
        className="rounded-lg border border-line px-3 py-1.5 text-sm transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:border-line disabled:text-muted disabled:hover:text-muted"
      >
        {plan.isPending ? 'Checking…' : label}
      </button>
      {plan.error && (
        <div className="mt-3">
          <ErrorNote error={plan.error} />
        </div>
      )}
    </div>
  )
}

/** The cost banner, worded as the CLI words it. */
function PlanBanner({ plan }: { plan: Plan }) {
  return (
    <div className="space-y-2 text-sm">
      <p className="text-warn">
        This step will launch LLM interactions, which might involve cost based on your
        configuration.
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 font-mono text-xs">
        <dt className="text-muted">backend</dt>
        <dd>
          {plan.backend} <BillingNote billing={plan.billing} />
        </dd>
        <dt className="text-muted">model</dt>
        <dd className="min-w-0 break-all">{plan.model}</dd>
        {plan.base_url && (
          <>
            <dt className="text-muted">endpoint</dt>
            <dd className="min-w-0 break-all">{plan.base_url}</dd>
          </>
        )}
        {plan.estimate && (
          <>
            <dt className="text-muted">estimate</dt>
            <dd>
              up to {plan.estimate.calls} LLM call(s)
              <span className="mt-0.5 block text-muted">{plan.estimate.basis}</span>
            </dd>
          </>
        )}
      </dl>
      {(plan.details ?? []).map((detail) => (
        <p key={detail} className="text-xs text-muted">
          {detail}
        </p>
      ))}
      {(plan.warnings ?? []).map((warning) => (
        <p key={warning} className="text-xs text-warn">
          ⚠ {warning}
        </p>
      ))}
    </div>
  )
}

/** Three-state on purpose — an unknown gateway must not read as free. See `preflight.py`. */
function BillingNote({ billing }: { billing: Plan['billing'] }) {
  if (billing === 'local') return <span className="text-good">— no per-call charge</span>
  if (billing === 'billed') return <span className="text-warn">— bills per call</span>
  return <span className="text-warn">— Whetstone cannot tell whether this bills</span>
}

function JobStatus({
  job,
  onCancel,
  onDismiss,
}: {
  job: Job
  onCancel: () => void
  onDismiss: () => void
}) {
  const { completed, total, label } = job.progress
  const running = job.state === 'running'
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0

  return (
    <div className="rounded-lg border border-line bg-surface p-3 text-sm">
      <div className="flex flex-wrap items-center gap-3">
        <StateBadge state={job.state} />
        <span className="font-mono text-xs text-muted">{job.id}</span>
        {running ? (
          <button
            type="button"
            onClick={onCancel}
            className="ml-auto text-xs text-muted transition-colors hover:text-bad"
          >
            Cancel
          </button>
        ) : (
          <button
            type="button"
            onClick={onDismiss}
            className="ml-auto text-xs text-muted transition-colors hover:text-ink"
          >
            Dismiss
          </button>
        )}
      </div>

      {running && (
        <div className="mt-2">
          <div className="h-1 overflow-hidden rounded-full bg-line">
            {/* An indeterminate step (a gate scores both sides with no per-case hook) has total=1
                and would otherwise show a bar stuck at 0. Say what is happening instead. */}
            <div
              className="h-full bg-accent transition-[width] duration-500"
              style={{ width: `${total > 1 ? pct : 0}%` }}
            />
          </div>
          <p className="mt-1 text-xs text-muted">
            {total > 1 ? `${completed} of ${total}` : 'working'}
            {label && ` — ${label}`}
          </p>
        </div>
      )}

      {job.state === 'failed' && <p className="mt-2 text-xs text-bad">{job.error}</p>}
      {job.state === 'cancelled' && (
        <p className="mt-2 text-xs text-muted">Stopped. Nothing was recorded.</p>
      )}
      {job.state === 'done' && <JobResult job={job} />}
      <Transcript job={job} />
    </div>
  )
}

/**
 * What the model said, while it is still saying it.
 *
 * A progress bar reports that a call happened and nothing about what came back — and "what came
 * back" is the entire question during a run you are paying for. Every line here is material the
 * finished run's drill-down also shows; the difference is that this arrives while there is still
 * time to cancel and change something.
 */
function Transcript({ job }: { job: Job }) {
  const [open, setOpen] = useState(true)
  const box = useRef<HTMLDivElement>(null)
  const lines = job.log ?? []
  const dropped = job.log_dropped ?? 0

  // Follow the tail while it grows. Depending on the line count rather than on a timer means it
  // scrolls exactly when there is something new, and stays put once the job is finished.
  useEffect(() => {
    const el = box.current
    if (el && open) el.scrollTop = el.scrollHeight
  }, [lines.length, open])

  if (lines.length === 0) return null

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="text-xs text-muted transition-colors hover:text-ink"
      >
        {open ? '▾' : '▸'} what the model said ({lines.length}
        {dropped > 0 ? ` of ${lines.length + dropped}` : ''} lines)
      </button>
      {open && (
        <div
          ref={box}
          className="mt-1 max-h-72 overflow-y-auto rounded border border-line bg-canvas px-2 py-1.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap"
        >
          {dropped > 0 && (
            <p className="text-muted italic">
              … {dropped} earlier line{dropped === 1 ? '' : 's'} dropped; the run record keeps all
              of them
            </p>
          )}
          {lines.map((line, i) => (
            <p key={`${line.group}-${i}`} className={TONE[line.tone ?? 'plain']}>
              {line.text}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

const TONE: Record<string, string> = {
  plain: 'text-ink',
  said: 'text-muted',
  verdict: 'text-muted',
  ok: 'text-good',
  bad: 'text-bad',
}

function JobResult({ job }: { job: Job }) {
  const r = job.result as Record<string, unknown>
  if (job.kind === 'eval') {
    return (
      <p className="mt-2 text-xs">
        recall <span className="tabular">{fmt(r.recall)}</span> · fp{' '}
        <span className="tabular">{fmt(r.fp_rate)}</span> · {String(r.llm_calls)} call(s)
        {/* Which content was scored. Two runs of "the same skill" can differ only in this, and a
            number with no version attached is what made the editor screen contradict itself. The
            branch is named by role, not spelled out: `whetstone/skill/<id>` is forty characters
            that add nothing beside a score, and the gate transcript already settled this by
            labelling its sides `base`/`candidate` rather than repeating the ref on every line. */}
        {r.scored ? (
          <> · scored {r.scored === 'working tree' ? 'the working tree' : 'the draft'}</>
        ) : null}{' '}
        ·{' '}
        <a className="text-accent hover:underline" href={`/runs/${String(r.run_id)}`}>
          open the run
        </a>
      </p>
    )
  }
  if (job.kind === 'gate') {
    const reasons = (r.reasons as string[]) ?? []
    return (
      <div className="mt-2 text-xs">
        <p className={r.passed ? 'text-good' : 'text-bad'}>Gate: {r.passed ? 'PASS' : 'FAIL'}</p>
        {reasons.map((reason) => (
          <p key={reason} className="mt-0.5 text-muted">
            {reason}
          </p>
        ))}
        {Boolean(r.passed) && <p className="mt-1 text-muted">This content may now be proposed.</p>}
      </div>
    )
  }
  if (job.kind === 'review') {
    const found = Number(r.findings ?? 0)
    return (
      <p className="mt-2 text-xs">
        {found === 0 ? (
          <span className="text-muted">
            The skill said nothing about this change. Worth a{' '}
            <code className="font-mono">should_not_flag</code> case if that is right, and worth
            asking why if it is not.
          </span>
        ) : (
          <>
            {found} finding{found === 1 ? '' : 's'}, none ruled on yet ·{' '}
            <a className="text-accent hover:underline" href={`/reviews/${String(r.review_id)}`}>
              rule on them
            </a>
          </>
        )}
      </p>
    )
  }
  if (job.kind === 'update') {
    return (
      <p className="mt-2 text-xs text-muted">
        {String(r.note)}
        {Boolean(r.changed) && ' — staged; the skill needs a fresh gate.'}
      </p>
    )
  }
  return (
    <p className="mt-2 text-xs text-muted">
      drafted from {String(r.total_failures)} failure(s), shown as {String(r.shown)} cluster(s)
    </p>
  )
}

function StateBadge({ state }: { state: Job['state'] }) {
  const tone = { running: 'accent', done: 'good', failed: 'bad', cancelled: 'neutral' } as const
  return <Badge tone={tone[state]}>{state}</Badge>
}

function fmt(value: unknown): string {
  return typeof value === 'number' ? value.toFixed(3) : '—'
}
