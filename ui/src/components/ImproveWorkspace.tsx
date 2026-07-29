import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  useBatch,
  useBeginImprove,
  useConsoleConfig,
  useProposal,
  usePropose,
  useSaveGuidance,
  type PendingCase,
  type SkillDetail as Detail,
} from '@/api/client'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, score } from '@/components/primitives'

/**
 * The improve workspace: one place to take a skill from "triage promoted some cases it fails on"
 * to a gated, proposable guidance change.
 *
 * The loop the console was missing. Editing happened in a cramped box, and the LLM improve drafted
 * from whatever the last run happened to fail on rather than from the cases you just curated. Here
 * the branch is the artifact — edited by hand in your own editor (a shown `git worktree` command) or
 * by the LLM improve step, both committing to the same `whetstone/skill/<id>` — and every action is
 * scoped to the proposed cases you select.
 */
export function ImproveWorkspace({ detail }: { detail: Detail }) {
  const skillId = detail.skill.id
  const pending = detail.pending_cases
  const { data: config } = useConsoleConfig()
  const { data: proposal } = useProposal(skillId)
  const { data: batch } = useBatch()
  const begin = useBeginImprove(skillId)
  const save = useSaveGuidance()
  const propose = usePropose()
  const readOnly = Boolean(config?.read_only)

  const [selected, setSelected] = useState<ReadonlySet<string>>(
    () => new Set(pending.map((c) => c.id)),
  )
  const [batchRun, setBatchRun] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [notice, setNotice] = useState('')

  if (pending.length === 0) {
    return (
      <Empty>
        No cases waiting to work on. Promote some from <Link to="/triage">Triage</Link> first — the
        loop is: triage cases the skill fails on, then sharpen the guidance here until it catches
        them.
      </Empty>
    )
  }

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  // Holdout cases are scored but can never be gate-targeted (a change may not claim to fix a case
  // the improve loop never saw). Keep them out of the gate's targeted set so it is never refused,
  // and say why rather than let the run fail after minutes of model calls.
  const heldSelected = pending.filter((c) => selected.has(c.id) && c.holdout)
  const targetable = pending.filter((c) => selected.has(c.id) && !c.holdout).map((c) => c.id)

  return (
    <div className="max-w-3xl space-y-5">
      <BranchPanel
        skillId={skillId}
        branch={proposal?.branch ?? batch?.branch ?? ''}
        branchExists={proposal?.branch_exists ?? false}
        localEdit={proposal?.local_edit ?? ''}
        onBegin={() =>
          begin.mutate(undefined, {
            onSuccess: (r) =>
              setNotice(
                r.created ? `Created ${r.branch}.` : `${r.branch} already existed.`,
              ),
          })
        }
        beginning={begin.isPending}
        readOnly={readOnly}
        error={begin.error}
      />

      <section className="rounded-lg border border-line bg-surface p-4">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-sm font-medium">
            Proposed cases ({selected.size} of {pending.length} selected)
          </h3>
          <div className="flex gap-3 text-xs text-muted">
            <button type="button" onClick={() => setSelected(new Set(pending.map((c) => c.id)))}>
              all
            </button>
            <button
              type="button"
              onClick={() => setSelected(new Set(pending.filter(isFailing).map((c) => c.id)))}
              title="Select only the cases the last score got wrong"
            >
              failing
            </button>
            <button type="button" onClick={() => setSelected(new Set())}>
              none
            </button>
          </div>
        </div>
        <p className="mb-3 text-xs text-muted">
          Scoring, and the LLM improve, act on exactly these. Caught / missed is from the latest
          score of this batch.
        </p>
        <ul className="space-y-1.5">
          {pending.map((c) => (
            <li key={c.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
              <input
                type="checkbox"
                checked={selected.has(c.id)}
                onChange={() => toggle(c.id)}
                aria-label={`select ${c.id}`}
              />
              <Badge tone={c.kind === 'should_catch' ? 'accent' : 'neutral'}>
                {c.kind === 'should_catch' ? 'should catch' : 'should not flag'}
              </Badge>
              <span className="font-mono">{c.id}</span>
              <span className="font-mono text-xs text-muted">{c.path}</span>
              {c.holdout && (
                <Badge
                  tone="neutral"
                  title="Holdout — scored on every run, but the gate can't target it and the improve loop never learns from it"
                >
                  holdout
                </Badge>
              )}
              <span className="ml-auto">
                <CaseStatus c={c} />
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section className="space-y-3 rounded-lg border border-line bg-surface p-4">
        <div>
          <h3 className="text-sm font-medium">1 · Score against these</h3>
          <p className="mt-0.5 mb-2 text-xs text-muted">
            Runs the branch's guidance over the proposed cases. Missed / falsely-flagged cases are
            what to sharpen next; a merged case that regressed is what to protect.
          </p>
          <LaunchButton
            kind="eval"
            request={{ skill_id: skillId, scope: 'batch' }}
            label="Score against these"
            onDone={(job) => {
              const r = job.result as Record<string, unknown>
              setBatchRun(String(r.run_id ?? '') || null)
              setNotice(
                `Scored: recall ${fmt(r.recall)} · fp ${fmt(r.fp_rate)}` +
                  (r.run_id ? ` — open the run to see each case.` : ''),
              )
            }}
          />
          {batchRun && (
            <Link
              to={`/runs/${encodeURIComponent(batchRun)}`}
              className="ml-3 text-xs text-accent underline"
            >
              open the run →
            </Link>
          )}
        </div>

        <div className="border-t border-line pt-3">
          <h3 className="text-sm font-medium">2 · Sharpen the guidance</h3>
          <p className="mt-0.5 mb-2 text-xs text-muted">
            By hand on the branch (the command above), or draft a change with the LLM from the cases
            you selected. Either way it lands on the branch and is re-scored below.
          </p>
          <LaunchButton
            kind="improve"
            request={{ skill_id: skillId, run_id: batchRun, cases: [...selected] }}
            label="Improve from selected"
            onDone={(job) => {
              const r = job.result as Record<string, unknown>
              const body = String(r.body ?? '')
              const pages = (r.pages ?? {}) as Record<string, string>
              if (!body && !Object.keys(pages).length) {
                setNotice('The drafter proposed no change.')
                return
              }
              setDraft({
                body,
                pages,
                rationale: String(r.rationale ?? ''),
                selectedMissing: (r.selected_missing ?? []) as string[],
              })
            }}
          >
            <p className="text-xs text-muted">
              Drafts from the {selected.size} selected case(s)
              {batchRun ? '' : ' — score them first for the drafter to see what fails'}.
            </p>
          </LaunchButton>
          {draft && (
            <DraftReview
              draft={draft}
              staging={save.isPending}
              readOnly={readOnly}
              error={save.error}
              onStage={() =>
                save.mutate(
                  { skillId, edit: { body: draft.body, pages: draft.pages } },
                  {
                    onSuccess: () => {
                      setDraft(null)
                      setNotice('Staged onto the branch. Re-score to see if it caught them.')
                    },
                  },
                )
              }
              onDiscard={() => setDraft(null)}
            />
          )}
        </div>

        <div className="border-t border-line pt-3">
          <h3 className="text-sm font-medium">3 · Gate &amp; propose</h3>
          <p className="mt-0.5 mb-2 text-xs text-muted">
            When the selected cases pass and nothing regressed, prove it: the gate scores base vs the
            branch over the union, and the cases you selected must pass.
          </p>
          {heldSelected.length > 0 && (
            <p className="mb-2 text-xs text-muted">
              {heldSelected.map((c) => c.id).join(', ')} {heldSelected.length === 1 ? 'is' : 'are'}{' '}
              holdout — scored, but not gate-targeted (a change can&rsquo;t claim to fix a case the
              improve loop never sees).
            </p>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <LaunchButton
              kind="gate"
              request={{ skill_id: skillId, targeted: targetable }}
              label="Run the gate"
            />
            {proposal?.verdict.can_propose ? (
              <button
                type="button"
                disabled={readOnly || propose.isPending}
                onClick={() =>
                  propose.mutate(proposal.branch, {
                    onSuccess: (r) =>
                      setNotice(r.message + (r.merge_request_url ? ` — ${r.merge_request_url}` : '')),
                  })
                }
                className="rounded-lg border border-good/50 px-3 py-1.5 text-sm text-good transition-colors hover:bg-good/10 disabled:opacity-40"
              >
                Propose
              </button>
            ) : (
              <Badge tone="warn" title={proposal?.verdict.reason}>
                not gated yet
              </Badge>
            )}
          </div>
          {propose.error && <ErrorNote error={propose.error} />}
        </div>
      </section>

      {notice && (
        <p className="rounded-lg border border-good/40 bg-good/5 px-3 py-2 text-sm break-words">
          {notice}
        </p>
      )}
    </div>
  )
}

type Draft = {
  body: string
  pages: Record<string, string>
  rationale: string
  selectedMissing: string[]
}

function BranchPanel({
  skillId,
  branch,
  branchExists,
  localEdit,
  onBegin,
  beginning,
  readOnly,
  error,
}: {
  skillId: string
  branch: string
  branchExists: boolean
  localEdit: string
  onBegin: () => void
  beginning: boolean
  readOnly: boolean
  error: unknown
}) {
  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      <h3 className="text-sm font-medium">Branch</h3>
      <p className="mt-0.5 mb-2 text-xs text-muted">
        Every change to <span className="font-mono">{skillId}</span> — yours by hand and the LLM's —
        lands on one branch, and is gated before it can be proposed. The working tree is never
        touched.
      </p>
      {branchExists ? (
        <div className="space-y-2">
          <p className="font-mono text-xs text-muted">{branch}</p>
          <div>
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <p className="text-xs text-muted">Edit the files in your own editor:</p>
              <CopyButton text={localEdit} />
            </div>
            <pre className="overflow-x-auto rounded border border-line bg-canvas px-2 py-1.5 font-mono text-xs">
              {localEdit}
            </pre>
            <p className="mt-1 text-xs text-muted">
              Commit there; the LLM improve below also commits here, so <code>git pull</code> in the
              worktree shows its draft.
            </p>
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          <p className="text-sm text-muted italic">
            Not started. Create the branch to check it out and edit locally.
          </p>
          <button
            type="button"
            onClick={onBegin}
            disabled={readOnly || beginning}
            className="rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10 disabled:opacity-40"
          >
            {beginning ? 'Starting…' : 'Start improving'}
          </button>
        </div>
      )}
      {error != null && <ErrorNote error={error} />}
    </section>
  )
}

function DraftReview({
  draft,
  staging,
  readOnly,
  error,
  onStage,
  onDiscard,
}: {
  draft: Draft
  staging: boolean
  readOnly: boolean
  error: unknown
  onStage: () => void
  onDiscard: () => void
}) {
  const touched = Object.keys(draft.pages)
  return (
    <div className="mt-3 space-y-2 rounded-lg border border-accent/40 bg-accent/5 p-3">
      <p className="text-xs text-muted">
        Drafted{touched.length ? `, rewriting ${touched.join(', ')}` : ''}. Read it before staging —
        the drafter is not the reviewer.
      </p>
      {draft.rationale && <p className="text-sm">{draft.rationale}</p>}
      <pre className="max-h-64 overflow-auto rounded border border-line bg-canvas px-2 py-1.5 text-xs whitespace-pre-wrap">
        {draft.body}
      </pre>
      {draft.selectedMissing.length > 0 && (
        <p className="text-xs text-warn">
          Not drafted from: {draft.selectedMissing.join(', ')} — the score did not fail them (or they
          are holdout), so they were not shown to the drafter.
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onStage}
          disabled={readOnly || staging}
          className="rounded border border-good/50 px-3 py-1 text-sm text-good hover:bg-good/10 disabled:opacity-40"
        >
          {staging ? 'Staging…' : 'Stage onto branch'}
        </button>
        <button type="button" onClick={onDiscard} className="px-2 py-1 text-sm text-muted">
          Discard
        </button>
      </div>
      {error != null && <ErrorNote error={error} />}
    </div>
  )
}

/** Caught / missed for one pending case, read from its latest score. */
function CaseStatus({ c }: { c: PendingCase }) {
  if (c.kind === 'should_catch') {
    if (c.last_recall == null) return <span className="text-xs text-muted italic">not scored</span>
    return c.last_recall >= 1 ? (
      <Badge tone="good">caught</Badge>
    ) : (
      <Badge tone="warn">missed {score(c.last_recall, 2)}</Badge>
    )
  }
  if (c.last_fp_rate == null) return <span className="text-xs text-muted italic">not scored</span>
  return c.last_fp_rate <= 0 ? (
    <Badge tone="good">clean</Badge>
  ) : (
    <Badge tone="warn">flagged {score(c.last_fp_rate, 2)}</Badge>
  )
}

function isFailing(c: PendingCase): boolean {
  return c.kind === 'should_catch'
    ? c.last_recall != null && c.last_recall < 1
    : c.last_fp_rate != null && c.last_fp_rate > 0
}

function fmt(v: unknown): string {
  return typeof v === 'number' ? v.toFixed(2) : '—'
}

/** Copy a command to the clipboard, with a moment of confirmation. Localhost is a secure context,
 *  so the Clipboard API is available; a blocked write degrades to no-op rather than throwing. */
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      aria-label="Copy the checkout command"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(
          () => {
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          },
          () => undefined,
        )
      }}
      className="shrink-0 rounded border border-line px-2 py-0.5 text-xs text-muted transition-colors hover:border-accent/50 hover:text-accent"
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}
