import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ApiError,
  keys,
  useConsoleConfig,
  usePropose,
  useProposal,
  useSaveGuidance,
  type CaseSummary,
  type Proposal,
  type RunSummary,
  type SkillDetail,
} from '@/api/client'
import { Guidance } from './Guidance'
import { GuidanceDiff } from './GuidanceDiff'
import { LaunchButton } from './LaunchButton'
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
  const [pane, setPane] = useState<'diff' | 'preview'>('diff')

  const dirty = draft.trim() !== proposal.body.trim()
  const conflict = save.error instanceof ApiError && save.error.status === 409

  /**
   * Whether the case outcomes on this screen describe the guidance in the textarea.
   *
   * They often do not, and that was the gap. An eval scores the *working tree*; the editor and the
   * gate below it describe a *staged branch*. Once something is staged the two diverge, and the
   * screen shows a red MISSED directly beneath a change that already fixed it, with nothing saying
   * the two halves are about different versions. Gating does not close it either: a gate writes a
   * gate record, never a run record, so clearing a candidate at recall 1.00 leaves every case row
   * still reporting the baseline.
   *
   * Computed once here rather than in each panel, so the case list and the improve panel can never
   * disagree about it.
   */
  const scoredBy = detail.scored_by
  const outcomesAreStale = scoredBy != null && scoredBy.skill_hash !== proposal.skill_hash

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

      <ImprovePanel
        skillId={skillId}
        cases={detail.cases}
        onDrafted={setDraft}
        stale={outcomesAreStale}
        staged={proposal.staged}
        base={proposal.base}
      />

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
          {/* Said here because this box is not the whole guidance, and nothing else on the screen
              would tell you. A skill may split its rules across companion markdown; those pages are
              sent to the reviewer and are inside `skill_hash`, so editing one on disk invalidates a
              gate — which reads as the console spontaneously forgetting a passing verdict unless
              you know the page exists. */}
          {detail.skill.pages.length > 0 && (
            <p className="text-xs text-muted">
              Plus {detail.skill.pages.length} companion page(s), also sent to the reviewer and
              covered by the gate:{' '}
              {detail.skill.pages.map((p, i) => (
                <span key={p.path}>
                  {i > 0 && ', '}
                  <code className="font-mono">{p.path}</code>
                </span>
              ))}
              . Edit those on disk — this box is <code className="font-mono">SKILL.md</code> only.
            </p>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex items-baseline gap-3 text-xs">
            {/* Diff first, and selected by default while the draft differs: "what changed?" is the
                question at this moment, and the preview cannot answer it. */}
            <button
              type="button"
              onClick={() => setPane('diff')}
              className={`tracking-wide uppercase transition-colors ${
                pane === 'diff' ? 'text-ink' : 'text-muted hover:text-ink'
              }`}
            >
              Diff
            </button>
            <button
              type="button"
              onClick={() => setPane('preview')}
              className={`tracking-wide uppercase transition-colors ${
                pane === 'preview' ? 'text-ink' : 'text-muted hover:text-ink'
              }`}
            >
              Preview
            </button>
          </div>
          <div className="h-[28rem] overflow-y-auto rounded-lg border border-line bg-surface p-3">
            {pane === 'diff' ? (
              <GuidanceDiff before={proposal.body} after={draft} />
            ) : (
              <Guidance detail={{ ...detail, skill: { ...detail.skill, body: draft } }} />
            )}
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
      <PinnedCases
        cases={detail.cases}
        scoredBy={scoredBy}
        stale={outcomesAreStale}
        staged={proposal.staged}
        base={proposal.base}
        skillId={skillId}
      />
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

/**
 * Drafting a guidance change from what the last run got wrong.
 *
 * The proposal lands in the editor above rather than being committed: a draft is a suggestion, and
 * the person reading it is the one who decides whether it is an improvement. From there it takes
 * the ordinary path — stage, gate, propose — so nothing about a machine-written rule can skip a
 * step a hand-written one has to pass.
 */
function ImprovePanel({
  skillId,
  cases,
  onDrafted,
  stale,
  staged,
  base,
}: {
  skillId: string
  cases: CaseSummary[]
  onDrafted: (body: string) => void
  stale: boolean
  staged: boolean
  base: string
}) {
  const [instruction, setInstruction] = useState('')
  const [note, setNote] = useState('')

  const scored = cases.filter((c) => c.last_recall !== null || c.last_fp_rate !== null)
  const failing = scored.filter((c) =>
    c.kind === 'should_catch' ? (c.last_recall ?? 1) < 1 : (c.last_fp_rate ?? 0) > 0,
  )
  // These counts come from the last run, so while that run describes different content than the
  // draft they are not a fact about anything on this screen. Saying "passing all 3 cases" directly
  // above a warning that those numbers are about the wrong version is the same self-contradiction
  // ADR-019 exists to remove, reproduced one panel further down — so when stale, the warning is
  // the whole message.
  const summary = stale
    ? ''
    : !scored.length
      ? 'Never scored, so a draft would see the guidance and nothing else. Run the evals first.'
      : failing.length === 0
        ? `Passing all ${scored.length} scored case(s) — there is nothing here to learn from.`
        : `Failing ${failing.length} of ${scored.length} scored case(s): ` +
          failing
            .slice(0, 3)
            .map((c) => c.id)
            .join(', ') +
          (failing.length > 3 ? `, and ${failing.length - 3} more` : '')

  return (
    <section className="rounded-lg border border-line bg-surface/50 p-3">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">Draft a change from the last run</h3>
        <span className="text-xs text-muted">loads into the editor; commits nothing</span>
      </div>
      {/* A button with no stated reason to press it is what made this panel read as noise. */}
      {summary && <p className="mb-2 text-xs text-muted">{summary}</p>}
      {/* "Run the evals first" with a draft staged used to point only at the header's Run evals,
          which scores the working tree — everything except the change being worked on. Staleness
          cannot be detected with no run to compare against, so the offer hangs off the draft
          existing at all. */}
      {!scored.length && staged && (
        <div className="mb-2">
          <ScoreTheDraft
            skillId={skillId}
            note="Your draft is on the branch; the header's Run evals would score the working tree."
          />
        </div>
      )}
      {/* The server refuses this outright — `_run_for` rejects a run that scored different content
          than the working tree. Said here as well, because being told before the click why the
          button will not work is the difference between a guard rail and a wall, and because the
          failures it would learn from are ones the staged edit may already have fixed. */}
      {stale && (
        <div className="mb-2 space-y-2 rounded border border-warn/40 bg-warn/5 px-2.5 py-2">
          <p className="text-xs text-warn">
            The last run scored the guidance on <code className="font-mono">{base}</code>, not what
            you have staged, so this would draft from failures your edit may already have fixed —
            and the server refuses it for that reason. Score the draft first, then the failures it
            learns from are the ones you actually still have.
          </p>
          <ScoreTheDraft skillId={skillId} />
        </div>
      )}
      <LaunchButton
        kind="improve"
        request={{ skill_id: skillId, instruction }}
        label="Draft a change"
        onDone={(job) => {
          const body = String((job.result as Record<string, unknown>).body ?? '')
          if (!body) return
          onDrafted(body)
          const r = job.result as Record<string, unknown>
          setNote(
            `Drafted from ${String(r.total_failures)} failure(s). ${String(r.rationale ?? '')}`,
          )
        }}
      >
        <label className="block text-xs text-muted">
          Steer this run (optional)
          <input
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="e.g. focus on false positives in test files"
            className="mt-1 w-full rounded border border-line bg-surface px-2 py-1 text-sm text-ink outline-none focus:border-accent/60"
          />
        </label>
      </LaunchButton>
      {note && <p className="mt-2 text-xs text-muted">{note}</p>}
    </section>
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
  const skillId = proposal.skill_id
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

      {/* The gate runs here. Until it did, this panel stated the rule that blocks publishing and
          then sent you to a terminal to satisfy it — which made C6 read as an obstacle rather than
          as the step it is. */}
      {proposal.staged && !verdict.can_propose && !pendingEdit && (
        <div className="mt-3">
          {/* Named as the question it answers. "Run the gate" is the mechanism; "did that help?"
              is what the person who just staged a change actually wants to know — and the gate is
              the only thing that answers it, because a single score on the new guidance has no
              baseline to be better than. Scoring the skill here would read the working tree, which
              still holds the old text, and report that nothing changed. */}
          <p className="mb-2 text-xs text-muted">
            Did that help? Scoring this skill would measure the working tree, which still holds the
            old guidance. The gate scores both versions over the same cases and reports the
            difference — which is also the evidence needed to publish.
          </p>
          <LaunchButton kind="gate" request={{ skill_id: skillId }} label="Run the gate" />
          <details className="mt-2">
            <summary className="cursor-pointer text-xs text-muted hover:text-ink">
              or run it yourself
            </summary>
            <pre className="mt-1 overflow-x-auto rounded border border-line bg-bg p-2 font-mono text-xs">
              {gateCommand(proposal, repo)}
            </pre>
          </details>
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
/** How this case went last time it was scored, or that it never was. */
/**
 * Score what is on the skill's branch.
 *
 * Offered anywhere the screen has just admitted that its numbers do not describe the draft. Telling
 * someone their outcomes are about the wrong version and leaving them to find the remedy is half a
 * feature; before this the remedy was checking out the branch and running the CLI by hand.
 */
function ScoreTheDraft({ skillId, note }: { skillId: string; note?: string }) {
  return (
    <div className="space-y-1">
      <LaunchButton
        kind="eval"
        request={{ skill_id: skillId, staged: true }}
        label="Score the draft"
      />
      {note && <p className="text-[11px] text-muted">{note}</p>}
    </div>
  )
}

/**
 * Which run the verdicts beneath came from, and whether it describes what is being edited.
 *
 * The unstale line is provenance rather than a warning: "where do I see the score?" is answered by
 * naming the run and linking to it, and a verdict with no stated source invites the reader to
 * assume it means whatever the rest of the screen means.
 */
function Provenance({
  scoredBy,
  stale,
  staged,
  base,
  skillId,
}: {
  scoredBy: RunSummary | null | undefined
  stale: boolean
  staged: boolean
  base: string
  skillId: string
}) {
  if (!scoredBy) {
    // A never-scored skill *with* a draft is the trap this whole feature exists to remove: the only
    // visible control was the header's "Run evals", which scores the working tree and therefore
    // measures everything except the change being worked on. Staleness cannot be detected here —
    // there is no run to compare against — so the button is offered whenever a draft exists.
    return (
      <div className="mb-2 space-y-2">
        <p className="text-xs text-muted">
          Never scored — run the evals to find out which of these the guidance currently gets wrong.
        </p>
        {staged && (
          <ScoreTheDraft skillId={skillId} note="Scores the branch, not the working tree." />
        )}
      </div>
    )
  }

  const link = (
    <Link to={`/runs/${encodeURIComponent(scoredBy.id)}`} className="font-mono hover:text-accent">
      {scoredBy.id}
    </Link>
  )

  if (!stale) {
    return (
      <p className="mb-2 text-xs text-muted">
        Outcomes from run {link} · {when(scoredBy.created_at)}
      </p>
    )
  }
  return (
    <div className="mb-2 space-y-2 rounded border border-warn/40 bg-warn/5 px-2.5 py-2">
      <p className="text-xs text-warn">
        These verdicts are from run {link}, which scored the guidance on{' '}
        <code className="font-mono">{base}</code> — <strong>not what you have staged</strong>. Your
        draft has never been run against these cases, so a MISSED below may already be fixed.
      </p>
      {/* The fix for the question this warning provokes, next to the warning. Telling someone their
          numbers describe the wrong thing and leaving them to work out the remedy is half a
          feature — and the remedy used to be "check out the branch and run the evals by hand". */}
      <ScoreTheDraft skillId={skillId} />
    </div>
  )
}

function CaseVerdict({ c }: { c: CaseSummary }) {
  const value = c.kind === 'should_catch' ? c.last_recall : c.last_fp_rate
  if (value === null || value === undefined) {
    return <span className="text-muted italic">not scored</span>
  }
  const passing = c.kind === 'should_catch' ? value >= 1 : value <= 0
  return (
    <span className={passing ? 'text-good' : 'text-bad'}>
      {passing ? 'passing' : c.kind === 'should_catch' ? 'MISSED' : 'FALSE POSITIVE'}
    </span>
  )
}

function PinnedCases({
  cases,
  scoredBy,
  stale,
  staged,
  base,
  skillId,
}: {
  cases: CaseSummary[]
  scoredBy: RunSummary | null | undefined
  stale: boolean
  staged: boolean
  base: string
  skillId: string
}) {
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
      <Provenance scoredBy={scoredBy} stale={stale} staged={staged} base={base} skillId={skillId} />
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
            {/* Without this the list is decoration: which of these the skill currently gets wrong
                is the only thing that makes it worth reading while rewriting a rule. */}
            <span className="ml-auto">
              <CaseVerdict c={c} />
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
