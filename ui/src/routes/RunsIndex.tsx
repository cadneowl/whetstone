import { Link } from 'react-router-dom'
import { useRuns } from '@/api/client'
import { Badge, Empty, ErrorNote, Loading, score, when } from '@/components/primitives'

export function RunsIndex() {
  const { data, isLoading, error } = useRuns()

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data?.length) {
    return (
      <Empty>
        No runs recorded yet — <code className="font-mono">whetstone eval run</code> stores one.
      </Empty>
    )
  }

  return (
    <div>
      <h1 className="mb-4 text-lg font-semibold">Runs</h1>
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
    </div>
  )
}
