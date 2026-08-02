import { Link } from 'react-router-dom'
import { useReviews, type ReviewListItem } from '@/api/client'
import { ReviewAChange, ReviewList } from '@/components/reviews'
import { Empty, ErrorNote, Intro, Loading } from '@/components/primitives'

/**
 * Every skill's live reviews, in one queue.
 *
 * The per-skill view is the skill's own Reviews tab — a review belongs to exactly one skill, and
 * that is where the loop it feeds continues. This is the cross-skill queue: which of them is
 * waiting on a human, when you do not already know which skill you came to work on.
 *
 * Grouped by skill rather than interleaved by date, because the group is the unit of work. Ruling
 * six findings for one skill and then improving it is one sitting; ruling six findings scattered
 * across four skills is four.
 */
export function ReviewsIndex() {
  const { data, isLoading, error } = useReviews()

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />

  const groups = groupBySkill(data ?? [])

  return (
    <div>
      <header className="mb-4">
        <h1 className="text-lg font-semibold">Reviews</h1>
        <Intro>
          The skill's own output on a live change, waiting for a verdict — the opposite direction
          from Triage, which infers what a reviewer <em>should</em> have said. Open one and mark
          each finding Correct or False positive. Each ruling mints a case the skill must keep
          getting right, and the skill's own <em>Reviews</em> tab carries that straight into the
          improve loop.
        </Intro>
      </header>

      <div className="mb-5">
        <ReviewAChange />
      </div>

      {groups.length === 0 ? (
        <Empty>No reviews yet — run one above.</Empty>
      ) : (
        <div className="space-y-5">
          {groups.map((group) => (
            <section key={group.skillId}>
              <h2 className="mb-2 flex flex-wrap items-baseline gap-x-3 text-sm">
                {group.known ? (
                  <Link
                    to={`/skills/${encodeURIComponent(group.skillId)}?tab=reviews`}
                    className="font-medium hover:text-accent"
                  >
                    {group.skillId}
                  </Link>
                ) : (
                  <span className="font-medium text-muted">{group.skillId}</span>
                )}
                <span className="text-xs text-muted">
                  {group.pending > 0
                    ? `${group.pending} finding${group.pending === 1 ? '' : 's'} to rule`
                    : 'all ruled'}
                  {group.expired > 0 &&
                    ` · ${group.expired} expired against newer guidance`}
                </span>
                {group.known ? (
                  <Link
                    to={`/skills/${encodeURIComponent(group.skillId)}?tab=reviews`}
                    className="ml-auto text-xs text-muted underline decoration-dotted hover:text-accent"
                  >
                    the skill's own reviews →
                  </Link>
                ) : (
                  /* The record outlives the skill, correctly — a ruling on it was still a real
                     label. What it must not do is offer a route into a skill page that 404s. */
                  <span
                    className="ml-auto text-xs text-warn"
                    title="No skill by this id is in the registry — it was renamed, moved or deleted after these reviews ran."
                  >
                    no longer in the registry
                  </span>
                )}
              </h2>
              <ReviewList items={group.items} showSkill={false} />
            </section>
          ))}
        </div>
      )}
    </div>
  )
}

export type ReviewGroup = {
  skillId: string
  items: ReviewListItem[]
  pending: number
  expired: number
  known: boolean
}

/**
 * Reviews grouped by the skill that produced them, the ones needing a ruling first.
 *
 * Extracted and exported because the ordering claim is the screen's whole argument — work waiting
 * on a human outranks a settled record — and a component this repo has no renderer for cannot test
 * it. Within a group the server's newest-first order is left alone.
 *
 * `pending` counts only reviews the guidance has *not* moved past. A stale review's findings
 * describe a reviewer that no longer exists, so counting them here would sort a skill to the top of
 * a queue for work that no ruling can finish. They are counted separately instead, and said
 * separately.
 *
 * `known` is false when the skill has left the registry — renamed, moved, deleted. The record is
 * still real evidence and stays listed, but a group header linking into `/skills/<id>` would be a
 * 404 offered as the way forward, so the header stops being a link.
 */
export function groupBySkill(items: ReviewListItem[]): ReviewGroup[] {
  const by = new Map<string, ReviewListItem[]>()
  for (const item of items) {
    const list = by.get(item.summary.skill_id)
    if (list) list.push(item)
    else by.set(item.summary.skill_id, [item])
  }
  return [...by.entries()]
    .map(([skillId, group]) => ({
      skillId,
      items: group,
      pending: group.reduce((sum, i) => sum + (i.stale_skill ? 0 : i.summary.pending), 0),
      expired: group.filter((i) => i.stale_skill).length,
      // Absent on every item or none: the flag is a property of the skill, not the review.
      known: group.every((i) => i.skill_known),
    }))
    .sort((a, b) => b.pending - a.pending || a.skillId.localeCompare(b.skillId))
}
