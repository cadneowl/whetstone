import { Link } from 'react-router-dom'
import { useRuns, useSkills, type SkillSummary } from '@/api/client'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Intro, Loading, score, when } from '@/components/primitives'

export function RunsIndex() {
  const { data, isLoading, error } = useRuns()

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />

  return (
    <div>
      <header className="mb-4">
        <h1 className="text-lg font-semibold">Runs</h1>
        <Intro>
          Every score ever recorded, newest first — the history a skill's guidance is judged
          against. Start a fresh one below; open a past one to see, case by case, what the reviewer
          said and why the judge did or did not accept it.
        </Intro>
      </header>
      <ScoreASkill />

      {!data?.length ? (
        <Empty>No runs recorded yet — score a skill above to record one.</Empty>
      ) : (
        <ul className="space-y-1.5">
          {data.map(({ summary, stale_version }) => (
            <li key={summary.id}>
              <Link
                to={`/runs/${encodeURIComponent(summary.id)}`}
                className="flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm transition-colors hover:border-accent/50"
              >
                <span className="text-muted">{when(summary.created_at)}</span>
                <span className="font-medium">{summary.skill_id}</span>
                <span className="font-mono text-xs text-muted">v{summary.skill_version}</span>
                {stale_version && (
                  <Badge tone="warn" title="Another run shares this version with different content">
                    version reused
                  </Badge>
                )}
                {summary.practice_mode && <Badge tone="warn">practice</Badge>}
                <span className="ml-auto flex items-baseline gap-4">
                  <span className="tabular">recall {score(summary.recall, 2)}</span>
                  <span className="tabular">fp {score(summary.fp_rate, 2)}</span>
                  <span className="text-xs text-muted">k={summary.k}</span>
                  <span className="font-mono text-xs text-muted">{summary.model}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * Starting a run, on the screen named after runs.
 *
 * This page listed results and offered no way to produce one — the only launcher was inside a skill
 * tab that was *also* called Runs, so anyone reaching for the top-nav item landed on a dead end with
 * the right name. A run is per skill, so this asks which; that is the only reason it was not simply
 * a button here in the first place.
 */
function ScoreASkill() {
  const { data: skills } = useSkills()
  const scorable = (skills ?? []).filter((s) => s.catch_cases + s.noflag_cases > 0)

  if (scorable.length === 0) return null

  return (
    <section className="mb-5 rounded-lg border border-line bg-surface/50 p-3">
      <h2 className="mb-2 text-sm font-medium">Score a skill</h2>
      <ul className="space-y-2">
        {scorable.map((skill) => (
          <li key={skill.id} className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-sm">
              <Link to={`/skills/${encodeURIComponent(skill.id)}`} className="hover:text-accent">
                {skill.name || skill.id}
              </Link>
              <span className="ml-2 text-xs text-muted">{cases(skill)}</span>
              {skill.latest ? (
                <span className="tabular ml-2 text-xs text-muted">
                  last: recall {score(skill.latest.recall, 2)} · fp {score(skill.latest.fp_rate, 2)}
                </span>
              ) : (
                <span className="ml-2 text-xs text-muted">never scored</span>
              )}
            </div>
            <LaunchButton kind="eval" request={{ skill_id: skill.id }} label="Run evals" />
          </li>
        ))}
      </ul>
    </section>
  )
}

function cases(skill: SkillSummary): string {
  const total = skill.catch_cases + skill.noflag_cases
  return `${total} case${total === 1 ? '' : 's'}`
}
