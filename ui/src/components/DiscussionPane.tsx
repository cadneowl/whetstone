import { Link } from 'react-router-dom'
import type { CandidateCase } from '@/api/client'
import { Badge } from './primitives'
import { SignalBadge } from './signals'

/**
 * The review conversation a candidate came from.
 *
 * This is the evidence. Every other thing on the triage screen is the builder's *reading* of it —
 * a kind, a confidence, a one-line `semantic` — and the question triage exists to answer is whether
 * that reading was fair. Asking it without showing the thread asks someone to take the builder's
 * word for it, which is the one thing a human-in-the-loop step must never do.
 *
 * Deliberately not collapsed by default. A pane you have to open is a pane that gets skipped, and
 * the failure mode this guards against — promoting "see above" as a rule's ground truth — is
 * invisible precisely when nobody looks.
 */
export function DiscussionPane({ candidate }: { candidate: CandidateCase }) {
  const discussion = candidate.discussion
  const comments = discussion?.comments ?? []
  const suggestion = discussion?.suggestion ?? ''

  return (
    <section className="min-w-0 rounded-lg border border-line bg-surface">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-3 py-2">
        <SignalBadge id={candidate.provenance.human_signal} />
        <Badge tone={candidate.kind === 'should_catch' ? 'accent' : 'neutral'}>
          {candidate.kind === 'should_catch' ? 'should catch' : 'should not flag'}
        </Badge>
        {comments.length > 0 && (
          <Badge tone={discussion?.resolved ? 'good' : 'warn'}>
            {discussion?.resolved ? 'thread resolved' : 'thread open'}
          </Badge>
        )}
        <MergeRequestLink candidate={candidate} />
      </header>

      {(discussion?.mr_title || candidate.rationale) && (
        <div className="space-y-0.5 border-b border-line px-3 py-1.5 text-xs text-muted">
          {discussion?.mr_title && <p className="break-words">{discussion.mr_title}</p>}
          {/* Why the builder thinks this is a case — its argument, next to the evidence for it. */}
          {candidate.rationale && <p className="break-words italic">{candidate.rationale}</p>}
        </div>
      )}

      {comments.length === 0 && !suggestion ? (
        <Silence candidate={candidate} />
      ) : (
        <div className="divide-y divide-line">
          {comments.map((comment, i) => (
            <article key={i} className="px-3 py-2">
              <p className="text-xs font-semibold">{comment.author || 'unknown'}</p>
              {/* `break-words` and not `truncate`: a reviewer's point is the payload, and the one
                  in the third paragraph is often the one that matters. */}
              <p className="mt-0.5 text-sm break-words whitespace-pre-wrap">{comment.body}</p>
            </article>
          ))}

          {suggestion && (
            <div className="px-3 py-2">
              <p className="mb-1 flex flex-wrap items-center gap-2 text-[11px] tracking-wide text-muted uppercase">
                Proposed replacement
                <Badge tone={discussion?.suggestion_applied ? 'good' : 'warn'}>
                  {discussion?.suggestion_applied ? 'author applied it' : 'not applied'}
                </Badge>
              </p>
              <pre className="overflow-x-auto rounded border border-line bg-canvas p-2 font-mono text-xs">
                {suggestion}
              </pre>
            </div>
          )}
        </div>
      )}
    </section>
  )
}

/**
 * A candidate with no thread behind it, explained rather than left blank.
 *
 * Three quite different things land here and they must not read alike: a merge that nobody
 * commented on, a defect that shipped, and a finding somebody ruled on directly. Only the first is
 * an argument from silence, and saying so about the other two would be false.
 */
function Silence({ candidate }: { candidate: CandidateCase }) {
  const signal = candidate.provenance.human_signal

  if (candidate.provenance.source?.startsWith('synthetic-')) {
    return (
      <div className="space-y-1.5 px-3 py-3 text-sm text-muted">
        <p>
          There was never a conversation — this candidate was generated from{' '}
          <ParentCaseLink candidate={candidate} />, and its authority is entirely inherited from
          that case's evidence.
        </p>
        <p className="text-xs">
          {candidate.kind === 'should_not_flag'
            ? 'It is the parent diff reversed: the defect being removed. Promote it if a reviewer really should stay silent about this fix; reject it if the reversal is not a change anyone would ship.'
            : 'It is the parent defect under different names. Promote it if the mutation preserved the defect; reject it if the model changed what is actually wrong.'}
        </p>
      </div>
    )
  }

  if (signal === 'escaped defect') {
    return (
      <p className="px-3 py-3 text-sm text-muted">
        No review conversation — that is the signal. This change shipped a defect and nobody
        objected to it in review.
      </p>
    )
  }

  if (signal === 'finding confirmed' || signal === 'finding rejected') {
    return (
      <div className="space-y-1.5 px-3 py-3 text-sm text-muted">
        <p>
          There was no conversation to read. This came from{' '}
          <Link to="/reviews" className="underline decoration-dotted hover:text-accent">
            adjudicating the skill's own output
          </Link>{' '}
          on a live change — a person looked at the finding above and ruled on it directly.
        </p>
        <p className="text-xs">
          {signal === 'finding rejected'
            ? 'Nothing here rests on inference: the case asserts the reviewer must stay silent at this line, and the gate will refuse any guidance that brings the false positive back.'
            : "The expectation is still the reviewer's own message. Rewrite it below into a standalone description of the problem, or the case grades the reviewer against its own words and passes forever."}
        </p>
      </div>
    )
  }

  // A `should_catch` candidate is not an argument from silence, whatever the thread looks like:
  // something in the provenance said a reviewer *did* object here. This branch used to tell every
  // commentless candidate that "it asserts a reviewer should stay quiet", directly contradicting
  // the `should catch` badge two inches above it.
  if (candidate.kind === 'should_catch') {
    return (
      <div className="space-y-1.5 px-3 py-3 text-sm text-muted">
        <p>No inline comment was captured for this merge request.</p>
        <p className="text-xs">
          The signal is the outcome, not the conversation:{' '}
          <em>{signal || 'a human acted on this code'}</em>. That is real evidence, but it is
          second-hand here — read the diff and open the merge request before promoting, because the
          expectation below was written without a comment to draw on.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-1.5 px-3 py-3 text-sm text-muted">
      <p>Nobody left an inline comment on this merge request.</p>
      <p className="text-xs">
        The case therefore rests on silence: it asserts a reviewer should stay quiet here, which is
        only as true as the original review was thorough. Worth promoting when the code really is
        unremarkable, worth rejecting when nobody looked.
      </p>
    </div>
  )
}

/** The parent case a synthetic candidate derives from, as a link into its case page. */
function ParentCaseLink({ candidate }: { candidate: CandidateCase }) {
  const ref = candidate.provenance.ref ?? ''
  const slash = ref.indexOf('/')
  if (slash <= 0) return <span className="font-mono">{ref || 'a parent case'}</span>
  const skill = ref.slice(0, slash)
  const caseId = ref.slice(slash + 1)
  return (
    <Link
      to={`/skills/${encodeURIComponent(skill)}/cases/${encodeURIComponent(caseId)}`}
      className="font-mono underline decoration-dotted hover:text-accent"
    >
      {ref}
    </Link>
  )
}

function MergeRequestLink({ candidate }: { candidate: CandidateCase }) {
  const url = candidate.discussion?.mr_url
  const label = candidate.provenance.ref ?? 'source'
  if (candidate.provenance.source?.startsWith('synthetic-')) {
    return (
      <span className="ml-auto truncate text-xs">
        <ParentCaseLink candidate={candidate} />
      </span>
    )
  }
  if (!url) {
    return <span className="ml-auto truncate font-mono text-xs text-muted">{label}</span>
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="ml-auto truncate font-mono text-xs text-muted underline decoration-dotted hover:text-accent"
    >
      {label}
    </a>
  )
}
