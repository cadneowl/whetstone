import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useReviews, useSkills, type ReviewListItem, type ReviewSummary } from '@/api/client'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Intro, Loading, when } from '@/components/primitives'

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
        <Intro>
          The skill's own output on a live change, waiting for a verdict — the opposite direction
          from Triage, which infers what a reviewer <em>should</em> have said. Open one and mark
          each finding Correct or False positive. Each ruling mints a triage candidate: a confirmed
          finding becomes a case the skill must keep catching, a rejected one a case the gate
          refuses to let back in.
        </Intro>
      </header>

      <ReviewAChange />

      {!data?.length ? (
        <Empty>No reviews yet — run one above.</Empty>
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

/**
 * Running a review, on the screen that lists them.
 *
 * This was the one stage of the loop with no button: you could rule on findings but not produce
 * any, so a review had to come from the CLI or an upload — which meant the half of the corpus that
 * comes from the skill's own output was reachable only by people who already knew the commands.
 *
 * A pasted diff leads, because it works on the first day with nothing configured. The
 * merge-request field takes a URL pasted straight from the browser (or a bare number) and lets the
 * server fetch the diff; whether the forge is reachable is the server's to report, so the field is
 * always offered rather than hidden behind config the browser can only guess at.
 */
function ReviewAChange() {
  const { data: skills } = useSkills()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [skillId, setSkillId] = useState('')
  const [diff, setDiff] = useState('')
  const [mr, setMr] = useState('')

  const chosen = skillId || skills?.[0]?.id || ''
  const byMr = mr.trim().length > 0
  const request = byMr ? { skill_id: chosen, mr: mr.trim() } : { skill_id: chosen, diff }
  const ready = Boolean(chosen) && (diff.trim().length > 0 || byMr)

  if (!skills?.length) return null

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mb-4 rounded-lg border border-line px-3 py-1.5 text-sm transition-colors hover:border-accent/50 hover:text-accent"
      >
        Review a change
      </button>
    )
  }

  return (
    <section className="mb-5 space-y-3 rounded-lg border border-line bg-surface/50 p-3">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-medium">Review a change</h2>
        <span className="text-xs text-muted">
          the skill reads it and reports; nothing is judged and nothing is scored
        </span>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="ml-auto text-xs text-muted transition-colors hover:text-ink"
        >
          Close
        </button>
      </div>

      <label className="block text-xs text-muted">
        Skill
        <select
          value={chosen}
          onChange={(e) => setSkillId(e.target.value)}
          className="mt-1 block w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm text-ink"
        >
          {skills.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name || s.id}
            </option>
          ))}
        </select>
      </label>

      <label className="block text-xs text-muted">
        Paste a unified diff
        <textarea
          value={diff}
          onChange={(e) => setDiff(e.target.value)}
          rows={6}
          spellCheck={false}
          placeholder={'diff --git a/src/x.rs b/src/x.rs\n--- a/src/x.rs\n+++ b/src/x.rs\n@@ …'}
          className="mt-1 block w-full rounded border border-line bg-canvas px-2 py-1.5 font-mono text-xs text-ink"
        />
      </label>

      <label className="block text-xs text-muted">
        …or a GitLab merge-request URL or number
        <input
          value={mr}
          onChange={(e) => setMr(e.target.value)}
          placeholder="https://gitlab.example/acme/payments/-/merge_requests/1423"
          className="mt-1 block w-full rounded border border-line bg-canvas px-2 py-1.5 font-mono text-xs text-ink"
        />
        <span className="mt-1 block">
          Whetstone fetches the diff. A full URL needs only{' '}
          <code className="font-mono">[watch] gitlab_url</code> and a token; a bare number also
          needs <code className="font-mono">projects</code> set.
        </span>
      </label>

      {ready ? (
        <LaunchButton
          kind="review"
          request={request}
          label="Review it"
          onDone={(job) => {
            const id = (job.result as { review_id?: string }).review_id
            if (id) navigate(`/reviews/${encodeURIComponent(id)}`)
          }}
        />
      ) : (
        <p className="text-xs text-muted">
          Paste a diff, or give a merge request URL or number, to continue.
        </p>
      )}
    </section>
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
      {summary.title && <span className="min-w-0 flex-1 truncate text-muted">{summary.title}</span>}
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
