import { Link } from 'react-router-dom'
import { useSkills } from '@/api/client'
import { Badge, Empty, ErrorNote, Intro, Loading, Sparkline, score } from '@/components/primitives'

/**
 * The landing page, ordered weakest first.
 *
 * "Which of our skills is actually weak?" is the question this exists to answer, so the answer is
 * the sort order rather than something you assemble by running the CLI once per skill.
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
          {Boolean(data?.length) && <p className="text-xs text-muted">weakest first</p>}
        </div>
        <Intro>
          Every skill Whetstone can see, worst score first — so "which of ours is actually weak?" is
          the sort order rather than something you assemble by hand. Open one to read its guidance,
          edit it, see the cases holding it to account, and score it.
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
                      {/* The overfitting light: the latest run's train-vs-holdout pair. Green
                          means the skill performs on cases the improve loop has never seen;
                          a warn badge means the sharpening may be memorization. */}
                      {skill.holdout && (
                        <span
                          className="tabular text-xs text-muted"
                          title={`train ${score(skill.holdout.train_recall, 2)} vs holdout ${score(skill.holdout.holdout_recall, 2)} — the slice the improve loop never sees`}
                        >
                          hold {score(skill.holdout.holdout_recall, 2)}
                        </span>
                      )}
                      {skill.holdout && skill.holdout.divergence > 0.1 && (
                        <Badge tone="warn" title="Train runs well ahead of holdout — possible overfitting">
                          diverging
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
              {skill.description && <p className="mt-1 text-sm text-muted">{skill.description}</p>}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}
