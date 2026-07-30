import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  useConsoleConfig,
  useProposal,
  useSaveGuidance,
  type CaseSummary,
  type PendingCase,
  type Proposal,
  type RunSummary,
  type SkillDetail,
} from '@/api/client'
import { Guidance } from './Guidance'
import { GuidanceDiff } from './GuidanceDiff'
import { LaunchButton } from './LaunchButton'
import { Badge, ErrorNote, Loading, when } from './primitives'
import { SourceBadge } from './signals'

/** The skill's entry point. Every other guidance file is addressed by its path within the folder. */
const SKILL_FILE = 'SKILL.md'

/**
 * Editing a skill's guidance — the only screen in the console that changes what a reviewer does.
 *
 * Laid out so the three things a person needs while rewriting a rule are visible at once: the text,
 * how it will render, and the eval cases that constrain it (§10.2). The cases are pinned rather
 * than a click away because "will this rewrite still catch the unwrap case?" is the question the
 * editor exists to keep in front of you.
 *
 * Below that sits the gate-status panel (C6). It says whether a passing gate covers the exact
 * on-disk content, and when it does not, the reason is always stated — a change you should not
 * commit yet with no explanation reads as a bug, and this check is the whole point of the project.
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
  /**
   * Every file of the skill's guidance, keyed by path — `SKILL.md` plus its companion pages.
   *
   * A skill is a folder, and for many skills `SKILL.md` is a table of contents whose rules live in
   * `patterns/*.md`. Editing only `SKILL.md` meant the one box on this screen could not reach the
   * rule a failing case was about, and the page beside it said to go and edit the file on disk —
   * outside the console, outside the branch, and outside the gate that has to cover it.
   */
  const stagedFiles: Record<string, string> = { [SKILL_FILE]: proposal.body, ...proposal.pages }
  const [drafts, setDrafts] = useState(stagedFiles)
  const [active, setActive] = useState(SKILL_FILE)
  const [pane, setPane] = useState<'diff' | 'preview'>('diff')

  const draft = drafts[active] ?? ''
  const setDraft = (text: string) => setDrafts((d) => ({ ...d, [active]: text }))
  const edited = Object.keys(stagedFiles).filter(
    (p) => (drafts[p] ?? '').trim() !== stagedFiles[p]!.trim(),
  )
  const dirty = edited.length > 0

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
   *
   * Compared on the *guidance* hash, not the whole-skill one. The thing being edited on this
   * screen is the rules, so the question is whether the outcomes describe those rules — and a run
   * that scored them against cases still sitting on a triage batch does. Comparing whole-skill
   * identity called that run stale, which dead-ended the loop it exists for: promote cases, score
   * them, then be refused the draft because the working tree does not carry them yet.
   */
  const scoredBy = detail.scored_by
  const outcomesAreStale =
    scoredBy != null &&
    !!scoredBy.guidance_hash &&
    scoredBy.guidance_hash !== proposal.guidance_hash

  return (
    <div className="space-y-5">
      <p className="text-xs text-muted">
        Edits here write straight to <code className="font-mono">skills/{skillId}/</code> on disk —
        no branch, no commit. Commit and push with your own git when a change is gate-proven.
      </p>

      {/* Pending cases count here too. They are what the last run scored in the flow this panel
          exists for, so leaving them out made it announce "never scored, run the evals first"
          directly above a link to the run that had just scored them. */}
      <ImprovePanel
        skillId={skillId}
        cases={[...detail.cases, ...detail.pending_cases]}
        onDrafted={(body, pages) => {
          // Lands in the editor, uncommitted, exactly as a hand edit would.
          setDrafts((d) => ({ ...d, ...(body ? { [SKILL_FILE]: body } : {}), ...pages }))
          // Show the file it actually rewrote. A draft that only touched a page leaves `SKILL.md`
          // looking untouched, so staying here reads as "nothing happened" — and the response
          // carries a `body` either way, so its presence says nothing about what changed.
          const changedBody = !!body && body.trim() !== proposal.body.trim()
          const firstPage = Object.keys(pages)[0]
          if (!changedBody && firstPage) setActive(firstPage)
        }}
        stale={outcomesAreStale}
        staged={false}
        base={proposal.base}
        viaBatch={detail.pending_cases.length > 0}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <FileTabs
              files={Object.keys(stagedFiles)}
              active={active}
              edited={edited}
              onPick={setActive}
            />
            {dirty && <Badge tone="warn">{edited.length} unsaved</Badge>}
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
          {/* The whole folder is one unit of guidance: every file above reaches the reviewer and is
              inside `skill_hash`, so all of them are staged together and gated together. Worth
              saying, because a rule moving between two files here is invisible in any single one. */}
          {Object.keys(stagedFiles).length > 1 && (
            <p className="text-xs text-muted">
              This skill's guidance spans {Object.keys(stagedFiles).length} files. All of them go to the
              reviewer, all of them are covered by the gate, and staging commits them together — fix
              a rule in the file that holds it.
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
              <GuidanceDiff
                before={stagedFiles[active] ?? ''}
                after={draft}
              />
            ) : (
              // Previews the one file being edited, so `pages` is emptied: the panel renders the
              // whole folder, and here the folder is not what is on screen.
              <Guidance
                detail={{ ...detail, skill: { ...detail.skill, body: draft, pages: [] } }}
              />
            )}
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          disabled={!dirty || save.isPending}
          onClick={() =>
            save.mutate({
              skillId,
              // Every file, in one commit: a rule moved out of `SKILL.md` into a page is only
              // coherent if both halves land together.
              edit: {
                body: drafts[SKILL_FILE] ?? proposal.body,
                pages: Object.fromEntries(
                  Object.entries(drafts).filter(([path]) => path !== SKILL_FILE),
                ),
              },
            })
          }
          className="rounded-lg border border-accent/50 px-3 py-1.5 text-sm text-accent transition-colors hover:bg-accent/10 disabled:cursor-not-allowed disabled:border-line disabled:text-muted disabled:hover:bg-transparent"
        >
          {save.isPending ? 'Applying…' : 'Apply to disk'}
        </button>
        <button
          type="button"
          disabled={!dirty}
          onClick={() => setDrafts(stagedFiles)}
          className="text-sm text-muted transition-colors hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
        >
          Discard changes
        </button>
        <span className="text-xs text-muted">
          Writes to <code className="font-mono">skills/{skillId}/</code> on disk — commit with your
          own git.
        </span>
      </div>

      {save.error && <ErrorNote error={save.error} />}

      <ProposalPanel proposal={proposal} repo={repo} pendingEdit={dirty} />
      <PinnedCases
        pending={detail.pending_cases}
        cases={detail.cases}
        scoredBy={scoredBy}
        stale={outcomesAreStale}
        staged={false}
        base={proposal.base}
        skillId={skillId}
      />
    </div>
  )
}

/**
 * One gate metric, before → after, coloured only when it actually moved.
 *
 * A gate reports two numbers and a change usually moves one of them. Showing `1.00 → 1.00` in grey
 * beside `1.00 → 0.00` in green is what makes the pair readable at a glance: the unchanged one
 * recedes, and the improvement is the thing your eye lands on.
 */
function Moved({
  from,
  to,
  higherIsBetter = false,
}: {
  from: number
  to: number
  higherIsBetter?: boolean
}) {
  const better = higherIsBetter ? to > from : to < from
  const worse = higherIsBetter ? to < from : to > from
  return (
    <span className={better ? 'text-good' : worse ? 'text-bad' : undefined}>
      {from.toFixed(2)} → {to.toFixed(2)}
    </span>
  )
}

/**
 * The guidance files of one skill, as a row of tabs.
 *
 * `SKILL.md` first and the pages in path order, so the list is the same on every visit. A dot marks
 * a file with unsaved edits: with several files open, "unsaved" on its own does not say *where*,
 * and the one thing worse than not being able to edit a page is editing one and losing track of it.
 */
function FileTabs({
  files,
  active,
  edited,
  onPick,
}: {
  files: string[]
  active: string
  edited: string[]
  onPick: (path: string) => void
}) {
  const ordered = [
    ...files.filter((p) => p === SKILL_FILE),
    ...files.filter((p) => p !== SKILL_FILE).sort(),
  ]
  if (ordered.length === 1) {
    return <span className="text-xs tracking-wide text-muted uppercase">{SKILL_FILE}</span>
  }
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      {ordered.map((path) => (
        <button
          key={path}
          type="button"
          onClick={() => onPick(path)}
          className={`font-mono text-xs transition-colors ${
            path === active ? 'text-ink underline underline-offset-4' : 'text-muted hover:text-ink'
          }`}
        >
          {path}
          {edited.includes(path) && <span className="ml-1 text-warn">•</span>}
        </button>
      ))}
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
/**
 * Drafting a guidance change from what the last run got wrong.
 *
 * The proposal lands in the editor above rather than being applied: a draft is a suggestion, and
 * the person reading it is the one who decides whether it is an improvement. From there it takes
 * the ordinary path — apply to disk, gate — so nothing about a machine-written rule can skip a
 * step a hand-written one has to pass.
 */
function ImprovePanel({
  skillId,
  cases,
  onDrafted,
  stale,
  staged,
  base,
  viaBatch,
}: {
  skillId: string
  // Both kinds: the fields this panel reads — id, kind, and the two outcomes — are the ones a
  // pending case carries as well.
  cases: (CaseSummary | PendingCase)[]
  onDrafted: (body: string, pages: Record<string, string>) => void
  stale: boolean
  staged: boolean
  base: string
  viaBatch: boolean
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
            viaBatch={viaBatch}
            note="Not scored yet — score the guidance on disk to see how the edit does."
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
          <ScoreTheDraft skillId={skillId} viaBatch={viaBatch} />
        </div>
      )}
      <LaunchButton
        kind="improve"
        request={{ skill_id: skillId, instruction }}
        label="Draft a change"
        onDone={(job) => {
          const r = job.result as Record<string, unknown>
          const body = String(r.body ?? '')
          // Pages only where the step rewrote one, which is where the rule lived.
          const pages = (r.pages ?? {}) as Record<string, string>
          if (!body && !Object.keys(pages).length) return
          onDrafted(body, pages)
          const touched = Object.keys(pages)
          setNote(
            `Drafted from ${String(r.total_failures)} failure(s)` +
              (touched.length ? `, rewriting ${touched.join(', ')}` : '') +
              `. ${String(r.rationale ?? '')}`,
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

/** Gate status, made visible: whether a passing gate covers the on-disk guidance, and how to get one. */
function ProposalPanel({
  proposal,
  repo,
  pendingEdit,
}: {
  proposal: Proposal
  repo: string
  pendingEdit: boolean
}) {
  const { verdict } = proposal
  const evidence = verdict.evidence
  const skillId = proposal.skill_id
  // Unsaved edits above are not on disk yet, so a gate would measure the old text. Say "apply
  // first" rather than let someone gate a version the file does not hold.
  const reason = pendingEdit
    ? 'there are unsaved changes above — apply them to disk, then gate the result'
    : verdict.reason

  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h3 className="text-sm font-semibold">Gate status</h3>
        <span className="text-xs text-muted">
          v{proposal.version} · on disk, vs last committed at {proposal.base}
        </span>
        <span className="ml-auto">
          {!verdict.can_propose ? (
            <Badge tone="warn">not gated</Badge>
          ) : verdict.caveat ? (
            <Badge tone="warn" title={verdict.caveat}>
              gate-proven, with a caveat
            </Badge>
          ) : (
            <Badge tone="accent">gate-proven</Badge>
          )}
        </span>
      </div>

      {/* Both metrics, always. Reporting recall alone made a change that fixed a false positive —
          fp 1.00 → 0.00, the whole point of it — read as "recall 1.00 → 1.00", which is a line
          saying this accomplished nothing. */}
      {evidence && (
        <p className="mt-2 text-xs text-muted">
          Cleared by gate <code className="font-mono">{evidence.id}</code> ·{' '}
          {when(evidence.created_at)} · recall{' '}
          <Moved from={evidence.result.recall_old} to={evidence.result.recall_new} higherIsBetter />{' '}
          · fp <Moved from={evidence.result.fp_rate_old} to={evidence.result.fp_rate_new} />
          {evidence.result.fixed_cases.length > 0 &&
            ` · fixed ${evidence.result.fixed_cases.join(', ')}`}
        </p>
      )}

      {/* Shown even when gate-proven: a green badge over a history that disagrees with itself is the
          one thing this panel must not do. */}
      {verdict.caveat && (
        <p className="mt-2 rounded border border-warn/40 bg-warn/5 px-2.5 py-1.5 text-sm text-warn">
          {verdict.caveat}
        </p>
      )}

      {/* The gate runs here. It scores the on-disk guidance against the last committed version (or
          the naked model for a skill not committed yet) over the same cases, and reports the
          difference — the "did that help?" the editor actually wants. */}
      {!verdict.can_propose && (
        <div className="mt-3">
          {pendingEdit ? (
            <p className="mb-2 text-sm text-warn">{reason}</p>
          ) : (
            <>
              <p className="mb-2 text-xs text-muted">
                Did that help? The gate scores your last commit against what&rsquo;s on disk over the
                same cases and reports the difference — which is also the evidence to commit with
                confidence.
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
            </>
          )}
        </div>
      )}

      <p className="mt-3 text-xs text-muted">
        The console never commits or pushes. When it&rsquo;s gate-proven,{' '}
        <strong>commit and push with your own git</strong>.
      </p>
    </section>
  )
}

function gateCommand(proposal: Proposal, repo: string): string {
  return [
    'whetstone eval gate',
    `  --repo ${repo}`,
    `  --skill-path ${proposal.path}`,
    `  --base-ref ${proposal.base}`,
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
 * Score the on-disk guidance against the cases that actually exist.
 *
 * `draft` scores what is on disk — the guidance being edited, plus its graduated cases. When the
 * cases still live on the triage batch (nearly all of them — often every one), that is a run of
 * zero cases the console once offered to spend a model call on. `promoted` scores the same on-disk
 * guidance against the promoted set, so the button means what its label says at every point in the
 * loop.
 */
function ScoreTheDraft({
  skillId,
  note,
  viaBatch,
}: {
  skillId: string
  note?: string
  viaBatch: boolean
}) {
  return (
    <div className="space-y-1">
      <LaunchButton
        kind="eval"
        request={{ skill_id: skillId, scope: viaBatch ? 'promoted' : 'draft' }}
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
  pending,
}: {
  scoredBy: RunSummary | null | undefined
  stale: boolean
  staged: boolean
  base: string
  skillId: string
  // Whether this skill's cases live on a triage batch, which decides what scoring the draft has
  // to run against — see `ScoreTheDraft`.
  pending: boolean
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
          <ScoreTheDraft
            skillId={skillId}
            viaBatch={pending}
            note="Scores the branch, not the working tree."
          />
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
      <ScoreTheDraft skillId={skillId} viaBatch={pending} />
    </div>
  )
}

function CaseVerdict({ c }: { c: CaseSummary | PendingCase }) {
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
  pending,
  scoredBy,
  stale,
  staged,
  base,
  skillId,
}: {
  cases: CaseSummary[]
  pending: PendingCase[]
  scoredBy: RunSummary | null | undefined
  stale: boolean
  staged: boolean
  base: string
  skillId: string
}) {
  // Both the graduated cases and the promoted set waiting under `promoted_cases/`. A skill whose
  // cases are all still promoted-but-ungraduated is the normal state right after triage — telling
  // that operator to "promote some candidates" is telling them to redo the thing they just did.
  if (cases.length === 0 && pending.length === 0) {
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
        What constrains this guidance ({cases.length + pending.length})
      </h3>
      <Provenance
        scoredBy={scoredBy}
        stale={stale}
        staged={staged}
        base={base}
        skillId={skillId}
        pending={pending.length > 0}
      />
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
            <SourceBadge provenance={c.provenance} />
            <span className="font-mono text-muted">{c.path}</span>
            {/* Without this the list is decoration: which of these the skill currently gets wrong
                is the only thing that makes it worth reading while rewriting a rule. */}
            <span className="ml-auto">
              <CaseVerdict c={c} />
            </span>
          </li>
        ))}
      </ul>
      {/* Promoting writes cases to `promoted_cases/` on disk, separate from the eval corpus, so a
          skill an operator had just spent an afternoon adding cases to showed none of them — a list
          headed "what constrains this guidance" naming strictly less than what constrains it. Kept
          visually separate because they do not gate anything until graduated, but scored the same:
          the button below is what makes them worth listing rather than merely acknowledging. */}
      {pending.length > 0 && (
        <div className="mt-3 space-y-1">
          <p className="text-xs text-muted">
            Promoted from triage, waiting under <code className="font-mono">promoted_cases/</code>.
            They start gating changes to this guidance once graduated into the eval corpus; until
            then, score them here.{' '}
            {staged && <>This scores what you have staged, not {base}.</>}
          </p>
          <ul className="space-y-1">
            {pending.map((c) => (
              <li
                key={c.id}
                className="flex flex-wrap items-baseline gap-x-3 rounded border border-dashed border-line px-2.5 py-1.5 text-xs"
              >
                <Badge tone={c.kind === 'should_catch' ? 'accent' : 'neutral'}>
                  {c.kind === 'should_catch' ? 'catch' : 'no flag'}
                </Badge>
                <span className="font-mono">{c.id}</span>
                <SourceBadge provenance={c.provenance} />
                <span className="font-mono text-muted">{c.path}</span>
                <span className="ml-auto">
                  <CaseVerdict c={c} />
                </span>
              </li>
            ))}
          </ul>
          <ScoreTheBatch skillId={skillId} />
        </div>
      )}
    </section>
  )
}

/** Score the skill against the promoted cases waiting under `promoted_cases/`. */
function ScoreTheBatch({ skillId }: { skillId: string }) {
  return (
    <LaunchButton
      kind="eval"
      request={{ skill_id: skillId, scope: 'promoted' }}
      label="Score these pending cases"
    />
  )
}
