import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  useCancelJob,
  useJob,
  useLaunchJob,
  useModelChoice,
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
  const [armed, setArmed] = useState(false)
  // A backend chosen for *this* launch only (null = the console default). Kept here, not globally,
  // so running one step on another model never moves the default every other step inherits.
  const [model, setModel] = useState<{ provider: string; model: string } | null>(null)
  // The last cost plan that resolved. A re-plan (every model keystroke) briefly clears the
  // mutation's own `data`, and without a fallback the tall banner would collapse to a one-line
  // "Checking…" and back on each keystroke — the panel visibly shaking. Holding the previous plan
  // keeps the banner steady while the next estimate is in flight.
  const [lastPlan, setLastPlan] = useState<Plan | null>(null)
  const plan = usePlanJob(kind)
  const launch = useLaunchJob(kind)
  const cancel = useCancelJob()
  const { data: job } = useJob(jobId)

  // Fold a per-launch model choice into the request sent to both plan and launch, so the two never
  // disagree about which backend the confirmed run will use.
  const withModel = (r: JobRequest): JobRequest =>
    model ? { ...r, provider: model.provider, model: model.model } : r

  // A local/OpenAI provider has no default model, so a chosen provider with a blank model is an
  // incomplete choice: planning it only 422s. Anthropic resolves a blank to its own default, so it
  // is never incomplete. Skipping the plan keeps a guaranteed-to-fail request — and the red error
  // it surfaces — off the screen until there is a model to run.
  const incompleteModel = model != null && model.provider !== 'anthropic' && !model.model.trim()

  // Plan — and re-plan on a model change — while armed, debounced so typing a model id does not
  // spam the server. Driving it from here rather than from the model field's blur is deliberate: a
  // blur-triggered re-plan fires *as* you click Yes, which blanks the plan and disables the button
  // mid-click, so the launch takes two clicks. Nothing here reacts to the click.
  useEffect(() => {
    if (!armed || incompleteModel) return
    const id = setTimeout(() => plan.mutate(withModel(request), { onSuccess: setLastPlan }), 200)
    return () => clearTimeout(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [armed, incompleteModel, model?.provider, model?.model])

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
          setArmed(false)
          setModel(null)
          setLastPlan(null)
          plan.reset()
          launch.reset()
        }}
        // Re-arm in place: clear the finished job and go straight back to the cost plan, keeping the
        // per-run model choice. The two-click spend contract still holds — this only spares hunting
        // for the launch button the result panel replaced, which is the whole iterate loop's tempo.
        onRerun={() => {
          setJobId(null)
          setSeen(null)
          setLastPlan(null)
          plan.reset()
          launch.reset()
          setArmed(true)
        }}
      />
    )
  }

  if (armed) {
    // Prefer the live plan, fall back to the last one that resolved — so a re-plan never blanks
    // the banner (see `lastPlan`). Only a genuine first-ever plan shows "Checking…".
    const shownPlan = plan.data ?? lastPlan
    return (
      <div className="rounded-lg border border-warn/40 bg-warn/5 p-3">
        {incompleteModel ? (
          <p className="text-sm text-muted">
            Enter a model id for <span className="font-mono">{model!.provider}</span> below to see
            the cost.
          </p>
        ) : shownPlan ? (
          <PlanBanner plan={shownPlan} />
        ) : plan.isPending ? (
          <p className="text-sm text-muted">Checking what this will cost…</p>
        ) : null}
        {children && <div className="mt-3">{children}</div>}
        {/* Choose the backend for this one launch. Changing it re-plans (via the effect above), so
            the banner — its billing above all — always describes the model that would actually run. */}
        <LaunchModel value={model} onChange={setModel} />
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={!plan.data || incompleteModel || launch.isPending}
            onClick={() =>
              launch.mutate(withModel(request), {
                onSuccess: (started) => {
                  setJobId(started.id)
                  setArmed(false)
                },
              })
            }
            className="rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:text-muted"
          >
            {launch.isPending ? 'Starting…' : `Yes, ${label.toLowerCase()}`}
          </button>
          <button
            type="button"
            onClick={() => {
              setArmed(false)
              setModel(null)
              setLastPlan(null)
              plan.reset()
            }}
            className="text-sm text-muted transition-colors hover:text-ink"
          >
            Cancel
          </button>
        </div>
        {plan.error && !incompleteModel && (
          <div className="mt-3">
            <ErrorNote error={plan.error} />
          </div>
        )}
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
        disabled={disabled}
        title={disabled ? disabledReason : undefined}
        onClick={() => setArmed(true)}
        className="rounded-lg border border-line px-3 py-1.5 text-sm transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:border-line disabled:text-muted disabled:hover:text-muted"
      >
        {label}
      </button>
      {plan.error && (
        <div className="mt-3">
          <ErrorNote error={plan.error} />
        </div>
      )}
    </div>
  )
}

/**
 * Pick the backend for a single launch, defaulting to the console's current model.
 *
 * Only reports the choice via `onChange`; the parent's debounced effect is what re-plans, so the
 * banner tracks the choice without this needing to know about planning. The provider list is the
 * same closed set the header picker offers, minus any that need a base URL the browser cannot give
 * (`custom`) — offering an option that can only ever error is worse than not offering it. A base
 * URL is never entered here, so the browser can only choose among hosts Whetstone already knows.
 *
 * Two shapes of choice, and the difference matters for a gateway deployment:
 *   - `{provider:'', model:'x'}` — the console's own backend (its gateway/base URL), a different
 *     model. This is the model-only override the server keeps on the gateway; it is what you want
 *     when your default *is* a gateway and you only mean to change the model.
 *   - `{provider:'anthropic', model:…}` — a named provider, which deliberately leaves the
 *     configured backend and goes straight to that vendor. Powerful, but a footgun on a gateway
 *     deployment with no vendor key, so the hint says so plainly.
 *   - `null` — the pure console default, model and all.
 */
function LaunchModel({
  value,
  onChange,
}: {
  value: { provider: string; model: string } | null
  onChange: (v: { provider: string; model: string } | null) => void
}) {
  const { data } = useModelChoice()
  if (!data) return null
  // Drop providers that need a base URL the browser is not allowed to supply — `custom` is the one
  // preset with an OpenAI kind and no host, so it would 422 on every attempt. Anthropic has no host
  // either but does not need one, so it stays.
  const choosable = data.available.filter((b) => b.kind === 'anthropic' || b.base_url)
  const provider = value?.provider ?? ''
  const model = value?.model ?? ''
  const onDefault = provider === ''
  // Empty backend + empty model collapses to the pure default (null); anything else is an override.
  const set = (p: string, m: string) =>
    onChange(p === '' && m.trim() === '' ? null : { provider: p, model: m })
  // A named local provider (ollama and friends) has no default model, so a blank field there
  // errors; Anthropic and the default backend both resolve a blank. Word the placeholder to match.
  const needsModel = !onDefault && provider !== 'anthropic'
  const placeholder = onDefault
    ? `blank = ${data.resolved_model || 'default'}`
    : needsModel
      ? 'model id (required)'
      : 'blank = default'
  return (
    <div className="mt-3 border-t border-warn/20 pt-3">
      <label className="block text-xs text-muted">
        Model for this run
        {/* The input is always present — never appearing on selection — so the panel below it does
            not jump every time you touch the picker. */}
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <select
            value={provider}
            // Switching backend clears the model (ids differ per backend); staying keeps it.
            onChange={(e) => set(e.target.value, e.target.value === provider ? model : '')}
            className="rounded border border-line bg-canvas px-2 py-1 text-sm text-ink"
          >
            <option value="">
              Console default — {data.resolved_model || data.resolved_backend}
            </option>
            {choosable.map((b) => (
              <option key={b.name} value={b.name}>
                {b.label}
              </option>
            ))}
          </select>
          <input
            value={model}
            onChange={(e) => set(provider, e.target.value)}
            placeholder={placeholder}
            spellCheck={false}
            className="min-w-[10rem] flex-1 rounded border border-line bg-canvas px-2 py-1 font-mono text-xs text-ink"
          />
        </div>
      </label>
      <p className="mt-1 text-xs text-muted">
        {onDefault
          ? 'Runs on your configured backend — type a model to change only the model, still on it.'
          : 'Sends this one run straight to this provider, not your configured backend.'}
      </p>
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
  onRerun,
}: {
  job: Job
  onCancel: () => void
  onDismiss: () => void
  onRerun: () => void
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
          <div className="ml-auto flex items-center gap-3 text-xs">
            <button
              type="button"
              onClick={onRerun}
              className="text-muted transition-colors hover:text-accent"
            >
              Run again
            </button>
            <button
              type="button"
              onClick={onDismiss}
              className="text-muted transition-colors hover:text-ink"
            >
              Dismiss
            </button>
          </div>
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
        {Boolean(r.trace_diverged) && (
          <p
            className="mt-1 text-warn"
            title="This skill runs as an agent, and an agent chooses what to open. The two sides of this gate did not read the same things, so part of the difference may be how it investigated rather than the guidance itself. Open the gate record to compare the trajectories."
          >
            the two sides investigated differently — read this delta with that in mind
          </p>
        )}
        {Boolean(r.passed) && <p className="mt-1 text-muted">This content may now be proposed.</p>}
      </div>
    )
  }
  if (job.kind === 'judge-eval') {
    return (
      <p className="mt-2 text-xs">
        <span className={r.passed ? 'text-good' : 'text-bad'}>
          accuracy <span className="tabular">{fmt(r.accuracy)}</span>{' '}
          {r.passed ? 'clears' : 'misses'} the {fmt(r.bar)} bar
        </span>{' '}
        <span className="text-muted">
          over {String(r.total)} pair(s) — missed {String(r.missed)}, spurious{' '}
          {String(r.spurious)}
        </span>
      </p>
    )
  }
  if (job.kind === 'baseline') {
    const flagged = (r.flagged as string[]) ?? []
    return (
      <p className="mt-2 text-xs">
        {flagged.length === 0 ? (
          <span className="text-good">
            every case still discriminates — {String(r.testing_guidance)} of{' '}
            {String(r.active_catch)} catch case(s) fail with no guidance
          </span>
        ) : (
          <span className="text-bad">
            {flagged.length} saturated case{flagged.length === 1 ? '' : 's'} pass with no guidance
            at all: {flagged.join(', ')} — see the Health tab
          </span>
        )}
      </p>
    )
  }
  if (job.kind === 'drift') {
    const uncovered = (r.uncovered as string[]) ?? []
    return (
      <p className="mt-2 text-xs">
        {uncovered.length === 0 ? (
          <span className="text-good">
            coverage {fmt(r.coverage)} — every one of {String(r.recent_mrs)} recent MR(s) has a
            case within the similarity radius
          </span>
        ) : (
          <span className="text-warn">
            coverage {fmt(r.coverage)} over {String(r.recent_mrs)} recent MR(s) —{' '}
            {String(r.uncovered_total)} look like nothing in the corpus: {uncovered.join(', ')} —
            see the Health tab
          </span>
        )}
      </p>
    )
  }
  if (job.kind === 'synthesize') {
    const written = Number(r.written ?? 0)
    const skipped = (r.skipped as { case_id: string; reason: string }[]) ?? []
    return (
      <p className="mt-2 text-xs">
        {written > 0 ? (
          <>
            <span className="text-good">
              {written} candidate{written === 1 ? '' : 's'} written
            </span>{' '}
            <a className="text-accent hover:underline" href="/triage">
              — review them in triage
            </a>
          </>
        ) : (
          <span className="text-muted">
            nothing new{Number(r.existing ?? 0) > 0 && ' — the earlier output is still queued'}
          </span>
        )}
        {skipped.length > 0 && (
          <span className="mt-0.5 block text-muted">
            skipped {skipped.map((s) => `${s.case_id} (${s.reason})`).join('; ')}
          </span>
        )}
      </p>
    )
  }
  if (job.kind === 'index') {
    return (
      <p className="mt-2 text-xs text-muted">
        indexed {String(r.cases)} case(s) with <span className="font-mono">{String(r.model)}</span>{' '}
        — staged on the skill branch. The reviewer's context changed, so re-gate before proposing.
      </p>
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
