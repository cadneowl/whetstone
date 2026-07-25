import { Link } from 'react-router-dom'
import { useSkills } from '@/api/client'
import { Badge, Empty, ErrorNote, Loading, Sparkline, score } from '@/components/primitives'

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
  if (!data?.length) return <Empty>No skills found under the configured skills root.</Empty>

  return (
    <div>
      <div className="mb-4 flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">Skills</h1>
        <p className="text-xs text-muted">weakest first</p>
      </div>

      <ul className="space-y-2">
        {data.map((skill) => (
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
                  </span>
                  {skill.latest ? (
                    <>
                      <span className="tabular" title="recall">
                        R {score(skill.latest.recall, 2)}
                      </span>
                      <span className="tabular" title="false-positive rate">
                        FP {score(skill.latest.fp_rate, 2)}
                      </span>
                      <span className="text-accent">
                        <Sparkline values={skill.recall_trend} />
                      </span>
                    </>
                  ) : (
                    <span className="text-xs text-muted italic">never evaluated</span>
                  )}
                </div>
              </div>
              {skill.description && (
                <p className="mt-1 text-sm text-muted">{skill.description}</p>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}
