import * as Tabs from '@radix-ui/react-tabs'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  useSkill,
  type CaseSummary,
  type PendingCase,
  type SkillDetail as Detail,
} from '@/api/client'
import { Guidance } from '@/components/Guidance'
import { GuidanceEditor } from '@/components/GuidanceEditor'
import { HealthPanel } from '@/components/HealthPanel'
import { ImproveWorkspace } from '@/components/ImproveWorkspace'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Loading, score, when } from '@/components/primitives'

// The tab keys, in strip order. An unknown `?tab=` value coerces to the first — a blank content
// pane under a full tab strip reads as a broken page, and a bad link should land somewhere real.
const TAB_KEYS = ['guidance', 'edit', 'improve', 'cases', 'runs', 'health', 'meta'] as const

function activeTab(raw: string | null): string {
  return raw && (TAB_KEYS as readonly string[]).includes(raw) ? raw : 'guidance'
}

export function SkillDetail() {
  const { skillId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const { data, isLoading, error } = useSkill(skillId)

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  const { skill, cases, runs } = data
  const tab = activeTab(params.get('tab'))

  return (
    <div>
      <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-baseline gap-3">
            <h1 className="text-lg font-semibold">{skill.name || skill.id}</h1>
            <span className="font-mono text-sm text-muted">v{skill.version}</span>
            {skill.owner && <span className="text-sm text-muted">{skill.owner}</span>}
          </div>
          {skill.description && <p className="mt-1 text-sm text-muted">{skill.description}</p>}
          {skill.triggers.paths.length > 0 && (
            <p className="mt-2 font-mono text-xs text-muted">
              triggers: {skill.triggers.paths.join(', ')}
            </p>
          )}
        </div>
        {/* Scoring belongs on the header, not inside a tab. It is the most repeated action in the
            whole loop — you re-run it after every guidance edit — and it used to live behind the
            History tab, which is named for the records it produces rather than the thing it does.
            Here it is reachable from whichever tab you are on, Edit above all. */}
        <EvalLauncher detail={data} activeTab={tab} />
      </header>

      {/* The tab lives in the URL so the inbox can send you straight to the step it named, and
          so a reload lands where you were rather than back on the overview. An unknown value —
          a stale bookmark, or `?tab=history` when the key is `runs` — falls back to Guidance
          rather than rendering a blank pane under the tab strip. */}
      <Tabs.Root
        value={tab}
        onValueChange={(next) => setParams(next === 'guidance' ? {} : { tab: next }, { replace: true })}
      >
        <Tabs.List className="mb-4 flex gap-1 border-b border-line">
          <Trigger value="guidance">Guidance</Trigger>
          <Trigger value="edit">Edit</Trigger>
          <Trigger value="improve">Improve</Trigger>
          <Trigger value="cases">Eval cases ({cases.length})</Trigger>
          {/* "History", not "Runs": the top nav already has a Runs, and two screens by that name —
              only one of which could start a run — is how someone ends up on the wrong one. */}
          <Trigger value="runs">History ({runs.length})</Trigger>
          <Trigger value="health">Health</Trigger>
          <Trigger value="meta">Metadata</Trigger>
        </Tabs.List>

        <Tabs.Content value="guidance">
          <TabIntro>
            The rules as they stand on <code className="font-mono">main</code> — the exact prose the
            reviewer is given, and the only thing the improve loop ever changes. Each rule shows the
            merge requests that justified it.
          </TabIntro>
          <Guidance detail={data} />
        </Tabs.Content>

        <Tabs.Content value="edit">
          <TabIntro>
            Change the rules. Draft one from the last run's failures or write it yourself, read the
            diff, then Stage on branch — never the working tree, never{' '}
            <code className="font-mono">main</code>. Staged guidance cannot be proposed until a gate
            proves it broke nothing.
          </TabIntro>
          {/* Mounted only while selected, so the draft starts from what is on disk each time the
              tab is opened rather than from a stale copy taken at page load. */}
          <GuidanceEditor detail={data} />
        </Tabs.Content>

        <Tabs.Content value="improve">
          <TabIntro>
            The loop, in one place: score the skill against the cases you promoted from triage,{' '}
            <em>graduate</em> the ones that earn a place in the eval corpus, sharpen the guidance
            against the ones it still misses (by hand on the branch, or with the LLM), re-score, then
            gate and propose. Guidance edits land on the skill's branch — never the working tree.
          </TabIntro>
          <ImproveWorkspace detail={data} />
        </Tabs.Content>

        <Tabs.Content value="cases">
          <TabIntro>
            Real review outcomes this skill is held to: <em>should catch</em> means a human flagged
            it, <em>should not flag</em> means a human deliberately did not. They are what tells a
            better rule from a worse one — and what the gate measures a change against. Open one for
            the diff, the expectation and the merge request behind it.
          </TabIntro>
          {/* The empty-state is suppressed when a batch is pending: "No eval cases. Nothing gates a
              change" printed directly above a list of six is the self-contradiction to avoid — the
              pending section below states the true position, that they gate once the branch merges. */}
          {cases.length > 0 ? (
            <CaseTable skillId={skill.id} cases={cases} />
          ) : data.pending_cases.length === 0 ? (
            <Empty>No eval cases. Nothing gates a change to this skill's guidance.</Empty>
          ) : null}
          {/* Promoting writes cases to a batch branch, never to disk, so a skill an operator had
              just curated a set for showed none of them here. They do not gate anything until the
              branch merges, but they are scorable now — via "Promoted cases" in the header — and a
              cases tab that omits them is naming strictly less than what the skill is held to. */}
          {data.pending_cases.length > 0 && <PendingCaseList cases={data.pending_cases} />}
        </Tabs.Content>

        <Tabs.Content value="runs">
          <TabIntro>
            Every time this skill was scored. Open one to see, case by case, what the reviewer said
            and why the judge did or did not accept it — which is how you tell a bad rule from a bad
            eval case.
          </TabIntro>
          {cases.length === 0 && data.pending_cases.length === 0 && (
            <p className="mb-4 text-sm text-muted italic">
              No eval cases to score. Promote some from the triage queue first.
            </p>
          )}
          {cases.length === 0 && data.pending_cases.length > 0 && (
            <p className="mb-4 text-sm text-muted italic">
              No merged cases yet, but {data.pending_cases.length} promoted case(s) are waiting on
              the triage batch — score them with <em>Promoted cases</em> in the header.
            </p>
          )}

          {runs.length === 0 ? (
            <Empty>Never evaluated. Run evals above to record one.</Empty>
          ) : (
            <ul className="space-y-1.5">
              {/* The list is newest-first, so runs[i + 1] is the run before this one in time. A
                  judge change between the two is a measurement change: the same skill scored by a
                  different judge is a different number, and a trend read across the seam is
                  fiction. The seam is drawn rather than inferred-from-hover because the whole
                  point is to interrupt the eye that was about to compare. */}
              {runs.map((run, i) => {
                const older = runs[i + 1]
                const judgeChanged =
                  older != null && (older.judge_hash ?? '') !== (run.judge_hash ?? '')
                return (
                  <li key={run.id}>
                    <Link
                      to={`/runs/${encodeURIComponent(run.id)}`}
                      className="flex flex-wrap items-baseline gap-x-4 rounded-lg border border-line bg-surface px-3 py-2 text-sm transition-colors hover:border-accent/50"
                    >
                      <span className="text-muted">{when(run.created_at)}</span>
                      <span className="tabular">recall {score(run.recall, 2)}</span>
                      <span className="tabular">fp {score(run.fp_rate, 2)}</span>
                      <span className="text-xs text-muted">k={run.k}</span>
                      <span className="ml-auto font-mono text-xs text-muted">{run.model}</span>
                    </Link>
                    {judgeChanged && (
                      <p className="mt-1.5 border-t border-dashed border-line pt-1.5 text-center text-xs text-muted">
                        judge changed here — runs below were scored by a different judge, so their
                        numbers are not comparable with those above
                      </p>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </Tabs.Content>

        <Tabs.Content value="health">
          <TabIntro>
            The state of affairs in one look: the latest score with its train/holdout split, what
            the corpus is made of, cases ready to retire, the judge behind every number, and how
            the skill is doing on live reviews — the ground truth the scores are a proxy for.
          </TabIntro>
          <HealthPanel skillId={skill.id} />
        </Tabs.Content>

        <Tabs.Content value="meta">
          <TabIntro>
            Who owns this skill, which files it claims, and how much its scores are worth.{' '}
            <em>Precision evidence</em> is the one to read: a false-positive rate computed mostly
            from merges nobody commented on is measuring silence, not correctness.
          </TabIntro>
          <dl className="space-y-3 text-sm">
            <Field label="Owner">{skill.owner || <Muted>unset</Muted>}</Field>
            <Field label="Rules">
              {data.rules.length ? data.rules.join(', ') : <Muted>none declared</Muted>}
            </Field>
            <Field label="Trigger labels">
              {skill.triggers.labels.length ? (
                skill.triggers.labels.join(', ')
              ) : (
                <Muted>none</Muted>
              )}
            </Field>
            <Field label="Precision evidence">
              <PrecisionEvidence counts={data.precision_evidence} />
            </Field>
            <Field label="References">
              {skill.references.length === 0 ? (
                <Muted>none</Muted>
              ) : (
                <ul className="space-y-1">
                  {skill.references.map((ref, i) => (
                    <li key={i} className="font-mono text-xs">
                      {ref.kind}: {ref.repo ? `${ref.repo}/${ref.path}` : ref.id}
                    </li>
                  ))}
                </ul>
              )}
            </Field>
          </dl>
        </Tabs.Content>
      </Tabs.Root>
    </div>
  )
}

/** What the front-door "Run evals" control can be pointed at. Draft scoring lives in the Edit tab,
 *  where the unmerged rewrite it measures is on screen; the two front-door scopes answer "how does
 *  this skill do", not "did my edit help". */
type FrontScope = 'working' | 'batch'

/**
 * Score this skill from its header — and choose what gets scored.
 *
 * The old control fired one working-tree run and hid even that whenever the skill had no *merged*
 * cases. But "no merged cases" is the ordinary state right after triage: promoting writes cases to a
 * batch branch and never to disk, so an operator who had just curated a set landed on a skill page
 * with no way to run them. This offers the promoted batch as a first-class scope and makes it the
 * default whenever cases are waiting, so the thing you just did is the thing the button does.
 *
 * On the Improve tab the batch scope is dropped: that tab has its own "Score the promoted batch"
 * step, and two identical score buttons on one screen is the duplication to avoid. The header keeps
 * the working-tree scope there — "how does this skill do on disk" is a different question from "did
 * my staged change help", which the tab answers.
 */
function EvalLauncher({ detail, activeTab }: { detail: Detail; activeTab: string }) {
  const pending = detail.pending_cases.length
  const merged = detail.cases.length
  const onImproveTab = activeTab === 'improve'

  const scopes: { id: FrontScope; label: string; count: number; hint: string }[] = []
  // Batch first, so it is the default: a page with pending cases is a page reached straight from
  // promoting them. Suppressed on the Improve tab, which owns batch scoring.
  if (pending > 0 && !onImproveTab)
    scopes.push({
      id: 'batch',
      label: 'Promoted cases',
      count: pending,
      hint: `${pending} case(s) promoted from triage, waiting on the batch branch. Scores this skill against the set you just curated — the run the improve step then learns from.`,
    })
  if (merged > 0)
    scopes.push({
      id: 'working',
      label: 'Working tree',
      count: merged,
      hint: `${merged} merged case(s), scored as the guidance sits on disk.`,
    })

  const [scope, setScope] = useState<FrontScope>(scopes[0]?.id ?? 'working')
  if (scopes.length === 0) return null
  const selected = scopes.find((s) => s.id === scope) ?? scopes[0]!

  return (
    <div className="flex flex-col items-end gap-1.5">
      {scopes.length > 1 && (
        <div className="flex rounded-lg border border-line p-0.5 text-xs">
          {scopes.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => setScope(s.id)}
              className={`rounded-md px-2.5 py-1 transition-colors ${
                s.id === scope ? 'bg-accent/10 text-accent' : 'text-muted hover:text-ink'
              }`}
            >
              {s.label} <span className="tabular text-muted">· {s.count}</span>
            </button>
          ))}
        </div>
      )}
      {/* Keyed on scope so switching resets any open cost plan: the estimate is a function of the
          case count, which is exactly what the scope changes. */}
      <LaunchButton
        key={scope}
        kind="eval"
        request={{ skill_id: detail.skill.id, scope: selected.id }}
        label="Run evals"
      />
      <p className="max-w-xs text-right text-xs text-muted">{selected.hint}</p>
    </div>
  )
}

/**
 * The cases promoted from triage, waiting under `promoted_cases/` to be graduated.
 *
 * Rendered flat, not as links: the case-detail route loads from the eval corpus, and a promoted
 * case is not there yet — so a link would 404 until it is graduated.
 */
function PendingCaseList({ cases }: { cases: PendingCase[] }) {
  return (
    <div className="mt-5">
      <h3 className="mb-1 text-xs tracking-wide text-muted uppercase">
        Promoted from triage, not graduated yet ({cases.length})
      </h3>
      <p className="mb-2 max-w-3xl text-sm text-muted">
        Waiting under <code className="font-mono">promoted_cases/</code>. They begin gating changes
        to this guidance once graduated into the eval corpus — until then, score them with{' '}
        <em>Promoted cases</em> in the header, then graduate the ones that earn it on the{' '}
        <em>Improve</em> tab.
      </p>
      <ul className="space-y-1.5">
        {cases.map((c) => (
          <li
            key={c.id}
            className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-dashed border-line bg-surface px-3 py-2 text-sm"
          >
            <Badge tone={c.kind === 'should_catch' ? 'accent' : 'neutral'}>
              {c.kind === 'should_catch' ? 'should catch' : 'should not flag'}
            </Badge>
            <span className="font-mono">{c.id}</span>
            <span className="font-mono text-xs text-muted">{c.path}</span>
            <span className="ml-auto tabular text-muted">
              {c.kind === 'should_catch'
                ? `recall ${score(c.last_recall, 2)}`
                : `fp ${score(c.last_fp_rate, 2)}`}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function PrecisionEvidence({ counts }: { counts: Record<string, number> }) {
  const confirmed = counts.confirmed ?? 0
  const silence = counts.silence ?? 0
  const unclassified = counts.unclassified ?? 0
  const total = confirmed + silence + unclassified
  if (total === 0) return <Muted>no should-not-flag cases</Muted>

  return (
    <div className="space-y-1">
      <p>
        {confirmed} confirmed · {silence} from silence
        {unclassified > 0 && ` · ${unclassified} hand-written`}
      </p>
      {/* fp_rate averages over all of these. A case built from a clean merge only establishes that
          nobody commented, which is not the same as there being nothing to flag — so a skill whose
          precision rests mostly on those has an fp_rate that measures quietness as much as skill. */}
      {silence > confirmed && (
        <Badge tone="warn" title="fp_rate is mostly measuring that nobody commented">
          precision rests on silence
        </Badge>
      )}
    </div>
  )
}

function CaseTable({ skillId, cases }: { skillId: string; cases: CaseSummary[] }) {
  const archived = cases.filter((c) => c.tier === 'archive').length
  // The filter exists only once there is something to filter — a corpus with no archive yet
  // should not grow a control that does nothing.
  const [showArchive, setShowArchive] = useState(true)
  if (cases.length === 0) {
    return <Empty>No eval cases. Nothing gates a change to this skill's guidance.</Empty>
  }
  const shown = showArchive ? cases : cases.filter((c) => c.tier !== 'archive')
  return (
    <div>
      {archived > 0 && (
        <label className="mb-2 flex items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            checked={showArchive}
            onChange={(e) => setShowArchive(e.target.checked)}
          />
          show {archived} archived case{archived === 1 ? '' : 's'} — retired lessons kept as
          regression insurance, sampled at low weight
        </label>
      )}
      <ul className="space-y-1.5">
        {shown.map((c) => (
          <li key={c.id}>
            <Link
              to={`/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(c.id)}`}
              className={`flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm transition-colors hover:border-accent/50 ${
                c.tier === 'archive' ? 'opacity-60' : ''
              }`}
            >
              <Badge tone={c.kind === 'should_catch' ? 'accent' : 'neutral'}>
                {c.kind === 'should_catch' ? 'should catch' : 'should not flag'}
              </Badge>
              <span className="font-mono">{c.id}</span>
              {c.tier === 'archive' && (
                <Badge tone="neutral" title="Retired: drawn at low weight as regression insurance">
                  archived
                </Badge>
              )}
              {c.flaky && (
                <Badge tone="warn" title="Trials disagreed about this case">
                  flaky
                </Badge>
              )}
              <span className="font-mono text-xs text-muted">{c.path}</span>
              <span className="ml-auto tabular text-muted">
                {c.kind === 'should_catch'
                  ? `recall ${score(c.last_recall, 2)}`
                  : `fp ${score(c.last_fp_rate, 2)}`}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}

/**
 * What a tab is for, above its contents.
 *
 * Five tabs whose names are all nouns from the same domain — Guidance, Edit, Eval cases, History,
 * Metadata — read as five views of the same thing rather than five different jobs. One sentence
 * each is what turns them back into steps.
 */
function TabIntro({ children }: { children: React.ReactNode }) {
  return <p className="mb-4 max-w-3xl text-sm text-muted">{children}</p>
}

function Trigger({ value, children }: { value: string; children: React.ReactNode }) {
  return (
    <Tabs.Trigger
      value={value}
      className="-mb-px border-b-2 border-transparent px-3 py-2 text-sm text-muted transition-colors data-[state=active]:border-accent data-[state=active]:text-ink"
    >
      {children}
    </Tabs.Trigger>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-4">
      <dt className="w-32 shrink-0 text-muted">{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function Muted({ children }: { children: string }) {
  return <span className="text-muted italic">{children}</span>
}
