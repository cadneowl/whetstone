import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  useConsoleConfig,
  useReviews,
  useSkills,
  type ReviewListItem,
  type ReviewSummary,
} from '@/api/client'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Loading, when } from '@/components/primitives'

/**
 * Where a case minted from a review goes next.
 *
 * The loop's one missing wire. Ruling a finding writes a case to `promoted_cases/`, and until this
 * existed the console said so and stopped — "score it on the skill page" pointed at a tab strip,
 * leaving the operator to work out that the promoted set is scored from *Improve*, and to find the
 * case they had just made among everything else waiting there. The workspace already reads its
 * selection from `?cases=`, so naming the ids here lands on exactly the work that was just created.
 *
 * No ids means no `cases` param at all, which the workspace reads as "everything" — deliberately
 * not the empty selection, which means the same thing but says it in a way that looks like a bug.
 */
export function improveLink(skillId: string, caseIds: readonly string[] = []): string {
  const base = `/skills/${encodeURIComponent(skillId)}?tab=improve`
  return caseIds.length ? `${base}&cases=${caseIds.map(encodeURIComponent).join(',')}` : base
}

/** This skill's reviews tab: the queue is here, and so is the button that fills it. */
export function SkillReviews({ skillId }: { skillId: string }) {
  const { data, isLoading, error } = useReviews(skillId)

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />

  const items = data ?? []
  // Three sections, not two, and the split is by what to *do* rather than by state. A stale review
  // is not "waiting on a verdict" in any useful sense: the guidance it describes is gone, so the
  // console refuses to treat a ruling on it as current and its own detail page says to re-run it
  // instead. Listing it beside live work — which is what a `pending > 0` split does — puts the one
  // review you should not spend attention on at the top of the queue of things to spend attention
  // on, and inflates every count that queue is sorted by.
  const expired = items.filter((i) => i.stale_skill)
  const live = items.filter((i) => !i.stale_skill)
  const waiting = live.filter((i) => i.summary.pending > 0)
  const settled = live.filter((i) => i.summary.pending === 0)

  return (
    <div className="space-y-5">
      <ReviewAChange skillId={skillId} />

      {items.length === 0 ? (
        <Empty>
          No live reviews of this skill yet. Point it at an open merge request — or paste a diff —
          and it reports what it would say. Nothing is scored and nothing is judged; you rule on the
          findings, and each ruling mints a case the gate then holds the skill to.
        </Empty>
      ) : (
        <>
          {waiting.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs tracking-wide text-muted uppercase">
                Waiting on a verdict ({waiting.length})
              </h3>
              <p className="mb-2 max-w-3xl text-sm text-muted">
                Until someone rules on these, the corpus learns nothing from them — and they expire
                the moment you edit the guidance, because the findings then describe a reviewer that
                no longer exists.
              </p>
              <ReviewList items={waiting} />
            </section>
          )}

          {settled.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs tracking-wide text-muted uppercase">
                Ruled ({settled.length})
              </h3>
              <ReviewList items={settled} />
            </section>
          )}

          {expired.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs tracking-wide text-warn uppercase">
                Expired — the guidance moved on ({expired.length})
              </h3>
              <p className="mb-2 max-w-3xl text-sm text-muted">
                These ran against wording that is no longer on disk, so a ruling here would label a
                reviewer nobody runs. They are not counted as work waiting on you. Open one and{' '}
                <em>re-run it</em> — same change, current guidance — and rule on what it says now.
              </p>
              <ReviewList items={expired} />
            </section>
          )}
        </>
      )}

      {/* Only once there is something to have minted cases from. Above an empty queue it promises
          cases that do not exist, and the empty state has already explained the mechanism. */}
      {items.length > 0 && (
        <p className="max-w-3xl text-sm text-muted">
          Cases minted here land under <code className="font-mono">promoted_cases/</code> alongside
          anything promoted from triage.{' '}
          <Link to={improveLink(skillId)} className="text-accent underline">
            Sharpen the guidance against them on the Improve tab
          </Link>
          , then gate the change before you commit it.
        </p>
      )}
    </div>
  )
}

export function ReviewList({
  items,
  showSkill = false,
}: {
  items: ReviewListItem[]
  showSkill?: boolean
}) {
  return (
    <ul className="space-y-1.5">
      {items.map((item) => (
        <li key={item.summary.id}>
          <ReviewRow item={item} showSkill={showSkill} />
        </li>
      ))}
    </ul>
  )
}

export function ReviewRow({
  item,
  showSkill = true,
}: {
  item: ReviewListItem
  showSkill?: boolean
}) {
  const { summary } = item
  return (
    <Link
      to={`/reviews/${encodeURIComponent(summary.id)}`}
      className="flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm transition-colors hover:border-accent/50"
    >
      <span className="min-w-0 truncate font-mono text-xs">{summary.ref || summary.id}</span>
      {summary.title && <span className="min-w-0 flex-1 truncate text-muted">{summary.title}</span>}
      <Progress summary={summary} stale={item.stale_skill} />
      {item.stale_skill && (
        <Badge
          tone="warn"
          title="The guidance has been edited since this ran, so these findings describe a reviewer that no longer exists"
        >
          stale
        </Badge>
      )}
      <span className="ml-auto text-xs text-muted">{when(summary.created_at)}</span>
      {showSkill && <span className="font-mono text-xs text-muted">{summary.skill_id}</span>}
    </Link>
  )
}

function Progress({ summary, stale = false }: { summary: ReviewSummary; stale?: boolean }) {
  if (summary.findings === 0) return <Badge tone="neutral">no findings</Badge>
  if (summary.pending === 0) return <Badge tone="good">all {summary.findings} ruled</Badge>
  // "to rule" in accent is a call to action, and on an expired review it is the wrong one — the
  // section above it has just said these are not waiting on a verdict. State the fact instead.
  if (stale)
    return (
      <Badge tone="neutral" title="Not counted as work waiting on you — re-run it instead">
        {summary.pending} of {summary.findings} never ruled
      </Badge>
    )
  return (
    <Badge tone="accent">
      {summary.pending} of {summary.findings} to rule
    </Badge>
  )
}

/**
 * Running a review.
 *
 * This was the one stage of the loop with no button: you could rule on findings but not produce
 * any, so a review had to come from the CLI or an upload — which meant the half of the corpus that
 * comes from the skill's own output was reachable only by people who already knew the commands.
 *
 * A pasted diff leads, because it works on the first day with nothing configured. The
 * merge-request field takes a URL pasted straight from the browser (or a bare number) and lets the
 * server fetch the diff; whether the forge is reachable is the server's to report, so the field is
 * always offered rather than hidden behind config the browser can only guess at.
 *
 * `skillId` fixes the subject and drops the picker: on a skill's own tab, a select naming the skill
 * you are already looking at is a chance to review the wrong one.
 */
export function ReviewAChange({ skillId }: { skillId?: string }) {
  const { data: skills } = useSkills(!skillId)
  const { data: config } = useConsoleConfig()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [picked, setPicked] = useState('')
  const [diff, setDiff] = useState('')
  const [mr, setMr] = useState('')

  const chosen = skillId || picked || skills?.[0]?.id || ''
  const byMr = mr.trim().length > 0
  const request = byMr ? { skill_id: chosen, mr: mr.trim() } : { skill_id: chosen, diff }
  const ready = Boolean(chosen) && (diff.trim().length > 0 || byMr)
  const readOnly = Boolean(config?.read_only)

  if (!skillId && !skills?.length) return null

  if (!open) {
    return (
      <button
        type="button"
        // Disabled rather than hidden, and disabled *here* rather than only on the launch inside:
        // this button opens a form whose only ending is a 403, and filling one in to find that out
        // is worse than being told at the door. Matches how the launch buttons read on the same page.
        disabled={readOnly}
        title={
          readOnly
            ? 'This console is read-only, so it cannot start anything that writes.'
            : undefined
        }
        onClick={() => setOpen(true)}
        className="rounded-lg border border-line px-3 py-1.5 text-sm transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted disabled:hover:border-line disabled:hover:text-muted"
      >
        Review a change
      </button>
    )
  }

  return (
    <section className="space-y-3 rounded-lg border border-line bg-surface/50 p-3">
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

      {!skillId && skills && (
        <label className="block text-xs text-muted">
          Skill
          <select
            value={chosen}
            onChange={(e) => setPicked(e.target.value)}
            className="mt-1 block w-full rounded border border-line bg-canvas px-2 py-1.5 text-sm text-ink"
          >
            {skills.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name || s.id}
              </option>
            ))}
          </select>
        </label>
      )}

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
