import { Link } from 'react-router-dom'
import {
  useCheckNow,
  useInbox,
  type Attention,
  type ActionKind,
  type Signal,
  type WatchState,
} from '@/api/client'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Loading, score, when } from '@/components/primitives'

/**
 * The console's home: what happened since you last looked, and the one thing to do about it.
 *
 * Everything on this screen already existed somewhere — the candidate queue, the run history, the
 * gate verdict, the staged branch — spread across four pages, with the operator left to work out
 * which of ten possible actions was today's. This joins them into one row per skill and states the
 * next step and the reason for it.
 *
 * Rows are ordered by how close they are to shipping, not by how much is wrong: finishing a change
 * that already has a passing gate is worth more than starting a new one, and an inbox that buried
 * that under a longer list would be the list of everything the console already had.
 */
export function InboxRoute() {
  const { data, isLoading, error } = useInbox()

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return null

  const { inbox, watch } = data
  // Pydantic marks defaulted lists optional in the generated schema; they are always present.
  const rows = inbox.attention ?? []
  const busy = rows.filter((a) => a.action.kind !== 'nothing')
  const idle = rows.filter((a) => a.action.kind === 'nothing')

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">
            {busy.length === 0
              ? 'Nothing needs attention'
              : busy.length === 1
                ? '1 skill needs attention'
                : `${busy.length} skills need attention`}
          </h1>
          <p className="mt-1 text-sm text-muted">
            Whetstone watches your merge requests, turns what review caught — and what it missed —
            into eval cases, and measures whether a rule change actually helps.
          </p>
        </div>
        <WatchStatus watch={watch} />
      </header>

      {rows.length === 0 && (
        <Empty>
          No skills yet. A skill is a folder under your skills root with a{' '}
          <code className="font-mono">SKILL.md</code>.
        </Empty>
      )}

      {busy.length > 0 && (
        <ul className="space-y-2">
          {busy.map((row) => (
            <li key={row.skill_id}>
              <Row row={row} />
            </li>
          ))}
        </ul>
      )}

      {inbox.unrouted > 0 && (
        <p className="rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-sm text-warn">
          {inbox.unrouted} signal{inbox.unrouted === 1 ? '' : 's'} could not be matched to any
          skill — no skill's <code className="font-mono">triggers.paths</code> covers the files
          they touch.{' '}
          <Link to="/triage" className="underline">
            Review them in triage
          </Link>
          .
        </p>
      )}

      {idle.length > 0 && (
        <section>
          <h2 className="mb-2 text-xs tracking-wide text-muted uppercase">
            Up to date ({idle.length})
          </h2>
          <ul className="space-y-1">
            {idle.map((row) => (
              <li key={row.skill_id}>
                <Link
                  to={`/skills/${encodeURIComponent(row.skill_id)}`}
                  className="flex flex-wrap items-baseline gap-x-4 rounded-lg border border-line bg-surface px-3 py-2 text-sm transition-colors hover:border-accent/50"
                >
                  <span>{row.name}</span>
                  <span className="tabular text-muted">recall {score(row.recall, 2)}</span>
                  <span className="tabular text-muted">fp {score(row.fp_rate, 2)}</span>
                  <span className="ml-auto text-xs text-muted">{row.action.why}</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

/** One skill: what arrived, what is known about it, and the button for the next step. */
function Row({ row }: { row: Attention }) {
  const signals = row.signals ?? []
  return (
    <article className="rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <Link
          to={`/skills/${encodeURIComponent(row.skill_id)}`}
          className="font-medium hover:text-accent"
        >
          {row.name}
        </Link>
        <ActionBadge kind={row.action.kind} />
        {row.scored && (
          <span className="tabular text-xs text-muted">
            recall {score(row.recall, 2)} · fp {score(row.fp_rate, 2)}
            {row.failing_cases > 0 && ` · ${row.failing_cases}/${row.total_cases} failing`}
          </span>
        )}
        {row.stale_run && (
          <Badge tone="warn" title="The skill changed since it was last measured">
            stale
          </Badge>
        )}
      </div>

      <p className="mt-1 text-sm text-muted">{row.action.why}</p>

      {signals.length > 0 && <Signals signals={signals} total={row.new_signals} />}

      <div className="mt-3">
        <Action row={row} />
      </div>
    </article>
  )
}

/**
 * The evidence, not the count.
 *
 * "Four unwraps shipped in !812, !814 and !820" is a reason to change a rule; "4 candidates" is a
 * number. The merge request is what makes a signal checkable, so it is what the row leads with.
 */
function Signals({ signals, total }: { signals: Signal[]; total: number }) {
  return (
    <ul className="mt-2 space-y-1 border-l-2 border-line pl-3">
      {signals.map((signal) => (
        <li key={signal.candidate_id} className="text-xs">
          <span className={signal.kind === 'should_catch' ? 'text-bad' : 'text-muted'}>
            {signal.kind === 'should_catch' ? 'missed' : 'should stay quiet'}
          </span>{' '}
          <span className="font-mono">{signal.path}</span>
          {signal.ref && <span className="text-muted"> · {signal.ref}</span>}
          {signal.rationale && (
            <span className="block text-muted">{signal.rationale}</span>
          )}
        </li>
      ))}
      {total > signals.length && (
        <li className="text-xs text-muted">and {total - signals.length} more</li>
      )}
    </ul>
  )
}

/** The next step, as the thing that does it — never a link to a screen with a button on it. */
function Action({ row }: { row: Attention }) {
  const { kind, label } = row.action

  if (kind === 'triage') {
    return (
      <Link
        to="/triage"
        className="inline-block rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10"
      >
        {label} →
      </Link>
    )
  }
  if (kind === 'score') {
    return <LaunchButton kind="eval" request={{ skill_id: row.skill_id }} label={label} />
  }
  if (kind === 'gate') {
    return <LaunchButton kind="gate" request={{ skill_id: row.skill_id }} label={label} />
  }
  if (kind === 'improve') {
    // Drafting is a judgement call about text, so it belongs beside the editor and the diff
    // rather than behind a button on a summary screen.
    return (
      <Link
        to={`/skills/${encodeURIComponent(row.skill_id)}?tab=edit`}
        className="inline-block rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10"
      >
        {label} →
      </Link>
    )
  }
  if (kind === 'propose') {
    return (
      <Link
        to={`/skills/${encodeURIComponent(row.skill_id)}?tab=edit`}
        className="inline-block rounded-lg border border-good/50 px-3 py-1.5 text-sm text-good transition-colors hover:bg-good/10"
      >
        {label} →
      </Link>
    )
  }
  return null
}

function ActionBadge({ kind }: { kind: ActionKind }) {
  const tone = {
    propose: 'good',
    gate: 'accent',
    triage: 'accent',
    score: 'neutral',
    improve: 'warn',
    nothing: 'neutral',
  } as const
  return <Badge tone={tone[kind]}>{kind}</Badge>
}

/** When Whetstone last looked, and whether it is looking at all. */
function WatchStatus({ watch }: { watch: WatchState }) {
  const check = useCheckNow()
  const sweep = watch.last_sweep

  return (
    <div className="text-right text-xs text-muted">
      {!watch.enabled ? (
        <p>
          Not watching.{' '}
          <span className="font-mono">[watch] enabled = true</span> in whetstone.toml turns it on.
        </p>
      ) : watch.polling || check.isPending ? (
        <p className="text-accent">Checking…</p>
      ) : sweep ? (
        <p>
          {sweep.error ? (
            <span className="text-bad" title={sweep.error}>
              last check failed
            </span>
          ) : (
            <>
              checked {when(sweep.at)} · {sweep.found} new
            </>
          )}
        </p>
      ) : (
        <p>not checked yet</p>
      )}
      <button
        type="button"
        disabled={check.isPending || watch.polling}
        onClick={() => check.mutate()}
        className="mt-1 rounded border border-line px-2 py-0.5 transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted"
      >
        Check now
      </button>
      {check.error && (
        <div className="mt-2 text-left">
          <ErrorNote error={check.error} />
        </div>
      )}
    </div>
  )
}
