import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import {
  ApiError,
  keys,
  useConsoleConfig,
  usePropose,
  useProposal,
  useSaveGuidance,
  type CaseSummary,
  type Proposal,
  type SkillDetail,
} from '@/api/client'
import { Guidance } from './Guidance'
import { Badge, ErrorNote, Loading, when } from './primitives'

/**
 * Editing a skill's guidance — the only screen in the console that changes what a reviewer does.
 *
 * Laid out so the three things a person needs while rewriting a rule are visible at once: the text,
 * how it will render, and the eval cases that constrain it (§10.2). The cases are pinned rather
 * than a click away because "will this rewrite still catch the unwrap case?" is the question the
 * editor exists to keep in front of you.
 *
 * Below that sits the C6 panel. *Propose* is disabled until a passing gate exists for the exact
 * staged content, and the reason is always stated — a blocked action with no explanation reads as
 * a bug, and this one is the whole point of the project.
 */
export function GuidanceEditor({ detail }: { detail: SkillDetail }) {
  const { data: config } = useConsoleConfig()
  const { data: proposal, isLoading, error } = useProposal(detail.skill.id)

  if (config?.read_only) {
    return (
      <div className="space-y-4">
        <p className="rounded-lg border border-line bg-surface px-4 py-3 text-sm text-muted">
          The console is running read-only, so guidance cannot be edited here.
        </p>
        <Guidance detail={detail} />
      </div>
    )
  }

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!proposal) return null

  // Keyed on the staged content, so a successful save remounts the form against what is now on the
  // branch: the draft stops reading as unsaved, and "discard" goes back to the right thing.
  return (
    <Editor
      key={proposal.skill_hash}
      detail={detail}
      proposal={proposal}
      repo={config?.skills_repo ?? '.'}
    />
  )
}

function Editor({
  detail,
  proposal,
  repo,
}: {
  detail: SkillDetail
  proposal: Proposal
  repo: string
}) {
  const skillId = detail.skill.id
  const save = useSaveGuidance()
  const [draft, setDraft] = useState(proposal.body)

  const dirty = draft.trim() !== proposal.body.trim()
  const conflict = save.error instanceof ApiError && save.error.status === 409

  // A save answers with the branch's new head, and this component only remounts once the proposal
  // refetch lands. Without preferring the response, a second save in that window sends a head the
  // first save has already superseded and gets a 409 that is not a conflict with anyone.
  const expectHead = save.data?.proposal.head ?? proposal.head

  return (
    <div className="space-y-5">
      {proposal.staged && (
        <p className="text-xs text-muted">
          Editing what is staged on <code className="font-mono">{proposal.branch}</code>, not the
          merged version. The <em>Guidance</em> tab still shows what is on{' '}
          <code className="font-mono">{proposal.base}</code>.
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <div className="flex items-baseline justify-between">
            <label htmlFor="guidance-body" className="text-xs tracking-wide text-muted uppercase">
              SKILL.md
            </label>
            {dirty && <Badge tone="warn">unsaved</Badge>}
          </div>
          <textarea
            id="guidance-body"
            value={draft}
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
            className="h-[28rem] w-full resize-y rounded-lg border border-line bg-surface p-3 font-mono text-[13px] leading-relaxed outline-none focus:border-accent/60"
          />
          <p className="text-xs text-muted">
            Tag rules as <code className="font-mono">- **R1 — …**</code> so findings, provenance and
            the untested-guidance check can refer to them.
          </p>
        </div>

        <div className="space-y-2">
          <div className="text-xs tracking-wide text-muted uppercase">Preview</div>
          <div className="h-[28rem] overflow-y-auto rounded-lg border border-line bg-surface p-3">
            <Guidance detail={{ ...detail, skill: { ...detail.skill, body: draft } }} />
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={!dirty || save.isPending}
          onClick={() => save.mutate({ skillId, edit: { body: draft }, expectHead })}
          className="rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:border-line disabled:text-muted disabled:hover:bg-transparent"
        >
          {save.isPending ? 'Staging…' : 'Stage on branch'}
        </button>
        <button
          type="button"
          disabled={!dirty}
          onClick={() => setDraft(proposal.body)}
          className="text-sm text-muted transition-colors hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
        >
          Discard changes
        </button>
        <span className="text-xs text-muted">
          Commits to <code className="font-mono">{proposal.branch}</code> — never the working tree,
          never <code className="font-mono">{proposal.base}</code>.
        </span>
      </div>

      {conflict ? <Conflict skillId={skillId} error={save.error} /> : null}
      {save.error && !conflict && <ErrorNote error={save.error} />}

      <ProposalPanel proposal={proposal} repo={repo} pendingEdit={dirty} />
      <PinnedCases cases={detail.cases} />
    </div>
  )
}

/**
 * A stale write, shown as state rather than a toast.
 *
 * Not a three-way merge view — that is worth building when more than one person is actually
 * editing, and inventing a merge UI for a single-user console would be guessing at the problem.
 * What matters now is that the console never silently wins: the write was refused, the branch has
 * something this tab has not seen, and taking it is a deliberate act that discards the draft.
 */
function Conflict({ skillId, error }: { skillId: string; error: unknown }) {
  const client = useQueryClient()
  const problem = (error as ApiError).problem as { expected?: string; actual?: string }

  return (
    <div className="rounded-lg border border-warn/40 bg-warn/5 px-4 py-3 text-sm">
      <p className="text-warn">The branch moved since this tab read it, so nothing was written.</p>
      {problem.expected && problem.actual && (
        <p className="mt-1 font-mono text-xs text-muted">
          expected {problem.expected.slice(0, 8)} · found {problem.actual.slice(0, 8)}
        </p>
      )}
      <p className="mt-2 text-muted">
        Another tab, or a commit made by hand, staged an edit first. Loading it replaces the text
        above and discards this draft — copy anything you want to keep before pressing it.
      </p>
      <button
        type="button"
        onClick={() => void client.invalidateQueries({ queryKey: keys.proposal(skillId) })}
        className="mt-2 rounded-lg border border-line px-3 py-1 text-sm transition-colors hover:border-accent/50"
      >
        Load what is on the branch
      </button>
    </div>
  )
}

/** C6, made visible: what is staged, whether it may be published, and what would clear the block. */
function ProposalPanel({
  proposal,
  repo,
  pendingEdit,
}: {
  proposal: Proposal
  repo: string
  pendingEdit: boolean
}) {
  const propose = usePropose()
  const { verdict } = proposal
  const evidence = verdict.evidence
  // An unsaved draft is not what the branch holds, so a gate covering the branch says nothing
  // about what is on screen. Publishing now would push the *previous* text, which is the one
  // surprise this panel must never spring on anyone.
  const blocked = !verdict.can_propose || pendingEdit
  const reason = pendingEdit
    ? 'there are unsaved changes above — stage them, then gate the result'
    : verdict.reason

  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h3 className="text-sm font-semibold">Proposal</h3>
        <span className="font-mono text-xs text-muted">{proposal.branch}</span>
        <span className="text-xs text-muted">
          v{proposal.version} · {proposal.commits} commit{proposal.commits === 1 ? '' : 's'} ahead
          of {proposal.base}
        </span>
        <span className="ml-auto">
          {!verdict.can_propose ? (
            <Badge tone="warn">not gated</Badge>
          ) : verdict.caveat ? (
            <Badge tone="warn" title={verdict.caveat}>
              gated, with a caveat
            </Badge>
          ) : (
            <Badge tone="accent">gated</Badge>
          )}
        </span>
      </div>

      {evidence && (
        <p className="mt-2 text-xs text-muted">
          Cleared by gate <code className="font-mono">{evidence.id}</code> ·{' '}
          {when(evidence.created_at)} · recall {evidence.result.recall_old.toFixed(2)} →{' '}
          {evidence.result.recall_new.toFixed(2)}
          {evidence.result.fixed_cases.length > 0 &&
            ` · fixed ${evidence.result.fixed_cases.join(', ')}`}
        </p>
      )}

      {blocked && <p className="mt-2 text-sm text-warn">{reason}</p>}

      {/* Shown even when the proposal is permitted: a green badge over a history that disagrees
          with itself is the one thing this panel must not do. */}
      {verdict.caveat && (
        <p className="mt-2 rounded border border-warn/40 bg-warn/5 px-2.5 py-1.5 text-sm text-warn">
          {verdict.caveat}
        </p>
      )}

      {proposal.staged && !verdict.can_propose && (
        <div className="mt-3">
          <p className="text-xs text-muted">
            The console cannot launch runs yet, so gate it from a terminal:
          </p>
          <pre className="mt-1 overflow-x-auto rounded border border-line bg-bg p-2 font-mono text-xs">
            {gateCommand(proposal, repo)}
          </pre>
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={blocked || propose.isPending}
          title={blocked ? reason : undefined}
          onClick={() => propose.mutate(proposal.branch)}
          className="rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:border-line disabled:text-muted disabled:hover:bg-transparent"
        >
          {propose.isPending ? 'Pushing…' : 'Propose MR'}
        </button>
        {propose.data && <span className="text-sm text-muted">{propose.data.message}</span>}
      </div>
      {propose.error && (
        <div className="mt-3">
          <ErrorNote error={propose.error} />
        </div>
      )}
    </section>
  )
}

function gateCommand(proposal: Proposal, repo: string): string {
  return [
    'whetstone eval gate',
    `  --repo ${repo}`,
    `  --skill-path ${proposal.path}`,
    `  --base-ref ${proposal.base}`,
    `  --candidate-ref ${proposal.branch}`,
  ].join(' \\\n')
}

/**
 * The cases that constrain the rule being rewritten.
 *
 * Pinned beside the editor because a guidance change is only as trustworthy as what tests it: a
 * skill with two cases and a rewritten rule has a gate that will pass on almost anything.
 */
function PinnedCases({ cases }: { cases: CaseSummary[] }) {
  if (cases.length === 0) {
    return (
      <p className="rounded-lg border border-warn/40 bg-warn/5 px-4 py-3 text-sm text-warn">
        No eval cases. Nothing here can distinguish a better rule from a worse one — promote some
        candidates from the triage queue before editing this guidance.
      </p>
    )
  }
  return (
    <section>
      <h3 className="mb-2 text-xs tracking-wide text-muted uppercase">
        What constrains this guidance ({cases.length})
      </h3>
      <ul className="space-y-1">
        {cases.map((c) => (
          <li
            key={c.id}
            className="flex flex-wrap items-baseline gap-x-3 rounded border border-line px-2.5 py-1.5 text-xs"
          >
            <Badge tone={c.kind === 'should_catch' ? 'accent' : 'neutral'}>
              {c.kind === 'should_catch' ? 'catch' : 'no flag'}
            </Badge>
            <span className="font-mono">{c.id}</span>
            <span className="font-mono text-muted">{c.path}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
