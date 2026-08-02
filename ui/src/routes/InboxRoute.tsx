import { Link } from 'react-router-dom'
import {
  useCheckNow,
  useInbox,
  useSetTier,
  type Attention,
  type ActionKind,
  type Retirement,
  type Signal,
  type Sweep,
  type WatchState,
} from '@/api/client'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Intro, Loading, score, when } from '@/components/primitives'

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

  // The review backlog as a whole, which no row can show.
  //
  // This list names one action per skill, ranked by closeness to shipping — so a skill sitting on
  // four unruled findings *and* a staged change reads as `gate`, and its reviews shrink to a
  // secondary link. That is right for "what next for this skill" and useless for "I have an hour,
  // show me everything unruled", which is a mode this product needs precisely because reviews
  // expire: a mined candidate keeps, a review stops describing a reviewer that exists the moment
  // the guidance moves.
  const waiting = rows.reduce((sum, r) => sum + r.unruled_findings, 0)
  const waitingSkills = rows.filter((r) => r.unruled_findings > 0).length

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          {/* "Nothing needs attention" is only true if there is something to have an opinion
              about. On a console with no skills at all it read as all-is-well to the one person
              who most needed telling that nothing was set up. */}
          <h1 className="text-lg font-semibold">
            {rows.length === 0
              ? 'No skills yet'
              : busy.length === 0
                ? 'Nothing needs attention'
                : busy.length === 1
                  ? '1 skill needs attention'
                  : `${busy.length} skills need attention`}
          </h1>
          <Intro>
            {rows.length === 0 ? (
              <>
                This is the console's home: one row per skill, showing the single next thing worth
                doing and the evidence for saying so. Point{' '}
                <code className="font-mono">[skills] root</code> at a folder of skills to fill it.
              </>
            ) : (
              <>
                One row per skill, showing the single next thing worth doing and the evidence for
                saying so — ordered by closeness to shipping, not by how much is wrong. Start at the
                top; each row's button does the thing rather than sending you to a screen about it.
              </>
            )}
          </Intro>
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

      {/* Only once the backlog actually spans skills. With one skill waiting, that skill's own row
          already carries the button, and a second link to the same work would be noise. */}
      {waitingSkills > 1 && (
        <p className="text-sm text-muted">
          <Link to="/reviews" className="underline decoration-dotted hover:text-accent">
            {waiting} finding{waiting === 1 ? '' : 's'} waiting across {waitingSkills} skills →
          </Link>{' '}
          — every skill's live reviews in one queue, for when you are working the backlog rather
          than one skill.
        </p>
      )}

      {inbox.unrouted > 0 && (
        <p className="rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-sm text-warn">
          {inbox.unrouted} signal{inbox.unrouted === 1 ? '' : 's'} could not be matched to any skill
          — no skill's <code className="font-mono">triggers.paths</code> covers the files they
          touch.{' '}
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
        {/* Carried even when something else wins the row. An unruled finding is the only evidence
            here that expires — the guidance moves and it stops describing a reviewer that exists —
            so it must not be invisible behind every staged change. */}
        {row.unruled_findings > 0 && row.action.kind !== 'review' && (
          <Link
            to={`/skills/${encodeURIComponent(row.skill_id)}?tab=reviews`}
            className="text-xs text-muted underline decoration-dotted hover:text-accent"
            title="Findings on live reviews that nobody has ruled on — the strongest label the corpus can get, and it goes stale when the guidance changes"
          >
            {row.unruled_findings} unruled finding{row.unruled_findings === 1 ? '' : 's'}
          </Link>
        )}
        {/* The ones that already went stale. Never an action — a ruling cannot finish them, only a
            re-run can — but never silent either: this is a review someone paid for, and the reason
            it stopped counting is a guidance edit the operator made themselves. */}
        {row.stale_reviews > 0 && (
          <Link
            to={`/skills/${encodeURIComponent(row.skill_id)}?tab=reviews`}
            className="text-xs text-warn underline decoration-dotted hover:text-accent"
            title="These reviews ran against guidance that has since been edited, so their findings describe a reviewer that no longer exists. Re-run them to get a label worth having."
          >
            {row.stale_reviews} expired review{row.stale_reviews === 1 ? '' : 's'}
          </Link>
        )}
      </div>

      <p className="mt-1 text-sm text-muted">{row.action.why}</p>

      {signals.length > 0 && <Signals signals={signals} total={row.new_signals} />}
      {/* One list for both curation kinds — the evidence sentence carries the difference
          ("passed the last 10 gates" vs "passes with no guidance at all"). */}
      {row.action.kind === 'curate' && (
        <Retirements
          skillId={row.skill_id}
          retirements={[...(row.retirements ?? []), ...(row.saturated ?? [])]}
        />
      )}

      <div className="mt-3 flex flex-wrap items-start gap-3">
        <Action row={row} />
        {/* Re-scoring, offered alongside whatever today's headline action is. The inbox names one
            next step per skill on purpose, but "score it again" is the thing you want after every
            guidance edit, and a home screen that could only offer it to skills that had never been
            measured was hiding it from exactly the people who needed it most. */}
        {/* Never for a task skill: `Run evals` is the review path, which refuses one. Its own
            "run the tasks" is the row's headline action, so there is nothing missing here. */}
        {row.action.kind !== 'score' && row.total_cases > 0 && !row.is_task && (
          <LaunchButton kind="eval" request={{ skill_id: row.skill_id }} label="Run evals" />
        )}
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
          {signal.rationale && <span className="block text-muted">{signal.rationale}</span>}
        </li>
      ))}
      {total > signals.length && (
        <li className="text-xs text-muted">and {total - signals.length} more</li>
      )}
    </ul>
  )
}

/**
 * Retirement proposals, confirmable on the row.
 *
 * The evidence is the sentence — "passed the last 10 gates it appeared in" — so the decision can
 * be made here. Archive stages a one-line commit on the skill branch; C6 then asks for a fresh
 * gate before the changed corpus ships, which is why nothing here is automatic.
 */
function Retirements({ skillId, retirements }: { skillId: string; retirements: Retirement[] }) {
  const flip = useSetTier(skillId)
  return (
    <ul className="mt-2 space-y-1 border-l-2 border-line pl-3">
      {retirements.map((r) => (
        <li key={r.case_id} className="flex flex-wrap items-baseline gap-x-3 text-xs">
          <Link
            to={`/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(r.case_id)}`}
            className="font-mono hover:text-accent"
          >
            {r.case_id}
          </Link>
          <span className="text-muted">{r.evidence}</span>
          <button
            type="button"
            disabled={flip.isPending}
            onClick={() => flip.mutate({ caseId: r.case_id, tier: 'archive' })}
            className="rounded border border-line px-2 py-0.5 transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted"
          >
            {flip.isPending ? 'Archiving…' : 'Archive'}
          </button>
        </li>
      ))}
      {flip.error && (
        <li>
          <ErrorNote error={flip.error} />
        </li>
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
  if (kind === 'review') {
    // The skill's own Reviews tab, not the cross-skill queue: the row already named the skill, and
    // the tab is where ruling these leads on into scoring and sharpening them.
    return (
      <Link
        to={`/skills/${encodeURIComponent(row.skill_id)}?tab=reviews`}
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
    // A task skill is gated over its task cases; the review gate refuses it outright, so this row
    // used to offer a button whose only possible outcome was a 422.
    return (
      <LaunchButton
        kind={row.is_task ? 'task-gate' : 'gate'}
        request={{ skill_id: row.skill_id }}
        label={label}
      />
    )
  }
  if (kind === 'task') {
    // With cases, run them from here; without, the Tasks tab is where you find out what to add.
    return row.task_cases > 0 ? (
      <LaunchButton kind="task-eval" request={{ skill_id: row.skill_id }} label={label} />
    ) : (
      <Link
        to={`/skills/${encodeURIComponent(row.skill_id)}?tab=tasks`}
        className="inline-block rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10"
      >
        {label} →
      </Link>
    )
  }
  if (kind === 'improve') {
    // The Improve tab is the loop: score the failing cases, draft a change (by hand on the branch
    // or with the LLM), re-score, gate, propose. Drafting is a judgement call about text, and that
    // is where the text, the diff and the cases sit together.
    return (
      <Link
        to={`/skills/${encodeURIComponent(row.skill_id)}?tab=improve`}
        className="inline-block rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10"
      >
        {label} →
      </Link>
    )
  }
  if (kind === 'propose') {
    // Straight to the gate-and-propose step of the same loop.
    return (
      <Link
        to={`/skills/${encodeURIComponent(row.skill_id)}?tab=improve`}
        className="inline-block rounded-lg border border-good/50 px-3 py-1.5 text-sm text-good transition-colors hover:bg-good/10"
      >
        {label} →
      </Link>
    )
  }
  if (kind === 'curate' || kind === 'drift' || kind === 'cadence') {
    // The confirm buttons (curate), the uncovered list (drift) and the clocks (cadence) live on
    // the health tab; the row leads to the fuller picture rather than duplicating it.
    return (
      <Link
        to={`/skills/${encodeURIComponent(row.skill_id)}?tab=health`}
        className="inline-block rounded-lg border border-line px-3 py-1.5 text-sm text-muted transition-colors hover:border-accent/50 hover:text-accent"
      >
        Health →
      </Link>
    )
  }
  return null
}

function ActionBadge({ kind }: { kind: ActionKind }) {
  const tone = {
    propose: 'good',
    gate: 'accent',
    review: 'accent',
    triage: 'accent',
    score: 'neutral',
    improve: 'warn',
    drift: 'warn',
    curate: 'neutral',
    cadence: 'neutral',
    task: 'warn',
    nothing: 'neutral',
  } as const
  return <Badge tone={tone[kind]}>{kind}</Badge>
}

/** When Whetstone last looked, what it found, and whether it is looking at all. */
function WatchStatus({ watch }: { watch: WatchState }) {
  const check = useCheckNow()
  // The mutation's own result first: it is what this click just produced. Waiting for the refetched
  // inbox to carry it around would leave the button looking like it had done nothing.
  const sweep = check.data ?? watch.last_sweep
  const busy = check.isPending || watch.polling

  return (
    <div className="max-w-sm text-right text-xs text-muted">
      <p>
        {watch.enabled ? (
          <>Watching every {watch.interval_minutes} min.</>
        ) : (
          <>
            Not watching. <span className="font-mono">[watch] enabled = true</span> in
            whetstone.toml turns it on.
          </>
        )}
      </p>
      <button
        type="button"
        disabled={busy}
        onClick={() => check.mutate()}
        className="mt-1 rounded border border-line px-2 py-0.5 transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted"
      >
        {busy ? 'Checking…' : 'Check now'}
      </button>

      {/* Shown whether or not anything is watching on a schedule. `Check now` runs a real sweep
          either way, and this used to sit inside the `enabled` branch — so on the far commoner
          setup, where watching is off, clicking it reached out to a forge and reported absolutely
          nothing back. A button indistinguishable from a broken one. */}
      {check.error ? (
        <div className="mt-2 text-left">
          <ErrorNote error={check.error} />
        </div>
      ) : (
        sweep && !busy && <SweepResult sweep={sweep} />
      )}
    </div>
  )
}

/** What one sweep did, in the terms an operator would ask it in: what arrived, and when. */
function SweepResult({ sweep }: { sweep: Sweep }) {
  if (sweep.error) {
    return (
      <p className="mt-2 rounded border border-bad/40 bg-bad/5 px-2 py-1 text-left text-bad">
        {/* The reason, not a tooltip on the words "last check failed". An expired token and a
            project nobody configured need different things doing about them. */}
        Check failed at {when(sweep.at)}: {sweep.error}
      </p>
    )
  }

  const found = sweep.found ?? 0
  const queued = sweep.already_queued ?? 0
  const decided = sweep.already_decided ?? 0
  const skipped = sweep.skipped ?? []
  const detail = [
    found > 0 ? `${found} new` : 'nothing new',
    queued > 0 ? `${queued} already queued` : '',
    decided > 0 ? `${decided} already ruled on` : '',
    skipped.length > 0 ? `${skipped.length} unreachable` : '',
  ].filter(Boolean)

  return (
    <p className={`mt-2 ${found > 0 ? 'text-accent' : ''}`}>
      Checked {when(sweep.at)} · {detail.join(' · ')}
    </p>
  )
}
