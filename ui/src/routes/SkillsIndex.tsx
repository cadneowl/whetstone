import { Link } from 'react-router-dom'
import { useSkills, type RotStatus } from '@/api/client'
import { Badge, Empty, ErrorNote, Intro, Loading, Sparkline, score } from '@/components/primitives'

/**
 * The landing page, ordered worst first.
 *
 * "Which of our skills needs me?" is the question this exists to answer, so the answer is the sort
 * order and the rot strip rather than something you assemble by running the CLI once per skill. A
 * skill with a lit rot signal — drift, saturation, an overdue pass, a dead rule — sorts ahead of a
 * merely low score, because those are the calls the rest of the product detects but the score alone
 * would hide.
 */
export function SkillsIndex() {
  const { data, isLoading, error } = useSkills()

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />

  return (
    <div>
      {/* Header before the empty check, not after. An empty screen is the one a first-time
          operator sees, and returning bare `<Empty>` took the title and the explanation away from
          exactly the person who had never seen the screen before. */}
      <header className="mb-4">
        <div className="flex items-baseline justify-between">
          <h1 className="text-lg font-semibold">Skills</h1>
          {Boolean(data?.length) && <p className="text-xs text-muted">worst first</p>}
        </div>
        <Intro>
          Every skill Whetstone can see, worst first — a skill with a lit rot signal (drift,
          saturation, an overdue pass, a dead rule) sorts ahead of a merely low score, so "which of
          ours needs me?" is the order rather than something you assemble by hand. Open one to read
          its guidance, edit it, see the cases holding it to account, and score it.
        </Intro>
      </header>

      {!data?.length && (
        <Empty>
          No skills found under the configured skills root. A skill is a folder with a{' '}
          <code className="font-mono">SKILL.md</code> in it.
        </Empty>
      )}

      <ul className="space-y-2">
        {(data ?? []).map((skill) => (
          <li key={skill.id}>
            <Link
              to={`/skills/${encodeURIComponent(skill.id)}`}
              className="block rounded-lg border border-line bg-surface px-4 py-3 transition-colors hover:border-accent/50"
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="font-medium">{skill.name || skill.id}</span>
                <span className="font-mono text-xs text-muted">v{skill.version}</span>
                {skill.owner && <span className="text-xs text-muted">{skill.owner}</span>}
                {skill.stale_version && (
                  <Badge tone="warn" title="Another run shares this version with different content">
                    version reused
                  </Badge>
                )}

                <div className="ml-auto flex items-center gap-4 text-sm">
                  <span className="text-muted">
                    {skill.catch_cases} catch / {skill.noflag_cases} noflag
                    {skill.archive_cases > 0 && (
                      <span title="Retired cases, sampled at low weight as regression insurance">
                        {' '}
                        · {skill.archive_cases} archived
                      </span>
                    )}
                  </span>
                  {skill.latest ? (
                    <>
                      <span className="tabular" title="recall">
                        R {score(skill.latest.recall, 2)}
                      </span>
                      <span className="tabular" title="false-positive rate">
                        FP {score(skill.latest.fp_rate, 2)}
                      </span>
                      {/* The overfitting light: the latest run's train-vs-holdout pair. The
                          reading is the server's — `HoldoutReport.reading` — so this badge, the
                          status page and the sharpening report cannot disagree about whether an
                          alarm is sounding, and none of them fires on a gap smaller than the
                          holdout can resolve. */}
                      {skill.holdout && (
                        <span className="tabular text-xs text-muted" title={skill.holdout.reading}>
                          hold {score(skill.holdout.holdout_recall, 2)}
                        </span>
                      )}
                      {skill.holdout?.diverging && (
                        <Badge tone="warn" title={skill.holdout.reading}>
                          diverging
                        </Badge>
                      )}
                      {/* Not an alarm and not silence: a holdout too small to read is a reason the
                          rising line above is unconfirmed, and saying so is what points at the
                          fix — graduate more cases. */}
                      {skill.holdout?.unreadable && (
                        <Badge tone="neutral" title={skill.holdout.reading}>
                          too few to call
                        </Badge>
                      )}
                      <span className="text-accent">
                        <Sparkline values={skill.recall_trend} />
                      </span>
                    </>
                  ) : (
                    <span className="text-xs text-muted italic">never evaluated</span>
                  )}
                </div>
              </div>
              <RotStrip rot={skill.rot} />
              {skill.description && <p className="mt-1 text-sm text-muted">{skill.description}</p>}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * The rot traffic-light: the same signals the Health tab computes, reduced to what triage-at-a-
 * glance needs. Renders nothing when every light is quiet — an all-clear skill stays uncluttered,
 * and the strip's presence is itself the signal that something wants attention. `days since anchor`
 * is shown whenever known, since a stale anchor is context even when no clock is overdue yet.
 */
function RotStrip({ rot }: { rot: RotStatus }) {
  const anchor =
    rot.days_since_anchor !== null && rot.days_since_anchor !== undefined
      ? `anchored ${rot.days_since_anchor}d ago`
      : null
  if (rot.signals === 0 && !anchor) return null
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-2">
      {rot.drift_alarm && (
        <Badge tone="warn" title="The latest drift probe read past the alarm — the corpus stopped resembling what ships">
          drift
        </Badge>
      )}
      {rot.saturated > 0 && (
        <Badge tone="warn" title="Active catch cases the naked model already passes — they measure nothing">
          {rot.saturated} saturated
        </Badge>
      )}
      {rot.cadence_due > 0 && (
        <Badge tone="warn" title="Overdue routine passes — distill, saturation probe, anchor run, or drift review">
          {rot.cadence_due} pass{rot.cadence_due === 1 ? '' : 'es'} due
        </Badge>
      )}
      {rot.dead_rules > 0 && (
        <Badge tone="warn" title="meta.yaml rules the evidence no longer stands behind — the distill pass's removal list">
          {rot.dead_rules} dead rule{rot.dead_rules === 1 ? '' : 's'}
        </Badge>
      )}
      {anchor && (
        <span
          className="text-xs text-muted"
          title="Days since the active corpus was last scored whole — sampled scores are estimates until re-anchored"
        >
          {anchor}
        </span>
      )}
    </div>
  )
}
