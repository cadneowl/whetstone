import { Link } from 'react-router-dom'
import { useReviews, type ReviewListItem, type ReviewSummary } from '@/api/client'
import { Badge, Empty, ErrorNote, Loading, when } from '@/components/primitives'

/**
 * Live reviews awaiting a ruling.
 *
 * The other direction from triage. Triage mines what humans said months ago and infers what the
 * reviewer should have said; this asks the reviewer directly, about a change nobody has labelled,
 * and a person rules on the answer.
 */
export function ReviewsIndex() {
  const { data, isLoading, error } = useReviews()

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />

  return (
    <div>
      <header className="mb-4">
        <h1 className="text-lg font-semibold">Reviews</h1>
        <p className="mt-1 text-sm text-muted">
          A skill run over a live change. Rule on each finding and it becomes an eval case the gate
          enforces.
        </p>
      </header>

      {!data?.length ? (
        <Empty>
          No reviews yet. Run{' '}
          <code className="font-mono">
            whetstone review --skill skills/&lt;id&gt; --base-url … --project … --mr 1423
          </code>{' '}
          to review an open merge request, or <code className="font-mono">--diff patch.diff</code>{' '}
          for a local patch.
        </Empty>
      ) : (
        <ul className="space-y-1.5">
          {data.map((item) => (
            <li key={item.summary.id}>
              <Row item={item} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Row({ item }: { item: ReviewListItem }) {
  const { summary } = item
  return (
    <Link
      to={`/reviews/${encodeURIComponent(summary.id)}`}
      className="flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm transition-colors hover:border-accent/50"
    >
      <span className="min-w-0 truncate font-mono text-xs">{summary.ref || summary.id}</span>
      {summary.title && (
        <span className="min-w-0 flex-1 truncate text-muted">{summary.title}</span>
      )}
      <Progress summary={summary} />
      {item.stale_skill && (
        <Badge
          tone="warn"
          title="The guidance has been edited since this ran, so these findings describe a reviewer that no longer exists"
        >
          stale
        </Badge>
      )}
      <span className="ml-auto text-xs text-muted">{when(summary.created_at)}</span>
      <span className="font-mono text-xs text-muted">{summary.skill_id}</span>
    </Link>
  )
}

function Progress({ summary }: { summary: ReviewSummary }) {
  if (summary.findings === 0) return <Badge tone="neutral">no findings</Badge>
  if (summary.pending === 0) return <Badge tone="good">all {summary.findings} ruled</Badge>
  return (
    <Badge tone="accent">
      {summary.pending} of {summary.findings} to rule
    </Badge>
  )
}
