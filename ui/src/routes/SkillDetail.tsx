import * as Tabs from '@radix-ui/react-tabs'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useSkill, type CaseSummary } from '@/api/client'
import { Guidance } from '@/components/Guidance'
import { GuidanceEditor } from '@/components/GuidanceEditor'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Loading, score, when } from '@/components/primitives'

export function SkillDetail() {
  const { skillId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const { data, isLoading, error } = useSkill(skillId)

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  const { skill, cases, runs } = data

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
        {cases.length > 0 && (
          <LaunchButton kind="eval" request={{ skill_id: skill.id }} label="Run evals" />
        )}
      </header>

      {/* The tab lives in the URL so the inbox can send you straight to the step it named, and
          so a reload lands where you were rather than back on the overview. */}
      <Tabs.Root
        value={params.get('tab') ?? 'guidance'}
        onValueChange={(tab) => setParams(tab === 'guidance' ? {} : { tab }, { replace: true })}
      >
        <Tabs.List className="mb-4 flex gap-1 border-b border-line">
          <Trigger value="guidance">Guidance</Trigger>
          <Trigger value="edit">Edit</Trigger>
          <Trigger value="cases">Eval cases ({cases.length})</Trigger>
          {/* "History", not "Runs": the top nav already has a Runs, and two screens by that name —
              only one of which could start a run — is how someone ends up on the wrong one. */}
          <Trigger value="runs">History ({runs.length})</Trigger>
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

        <Tabs.Content value="cases">
          <TabIntro>
            Real review outcomes this skill is held to: <em>should catch</em> means a human flagged
            it, <em>should not flag</em> means a human deliberately did not. They are what tells a
            better rule from a worse one — and what the gate measures a change against. Open one for
            the diff, the expectation and the merge request behind it.
          </TabIntro>
          <CaseTable skillId={skill.id} cases={cases} />
        </Tabs.Content>

        <Tabs.Content value="runs">
          <TabIntro>
            Every time this skill was scored. Open one to see, case by case, what the reviewer said
            and why the judge did or did not accept it — which is how you tell a bad rule from a bad
            eval case.
          </TabIntro>
          {cases.length === 0 && (
            <p className="mb-4 text-sm text-muted italic">
              No eval cases to score. Promote some from the triage queue first.
            </p>
          )}

          {runs.length === 0 ? (
            <Empty>Never evaluated. Run evals above to record one.</Empty>
          ) : (
            <ul className="space-y-1.5">
              {runs.map((run) => (
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
                </li>
              ))}
            </ul>
          )}
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
  if (cases.length === 0) {
    return <Empty>No eval cases. Nothing gates a change to this skill's guidance.</Empty>
  }
  return (
    <ul className="space-y-1.5">
      {cases.map((c) => (
        <li key={c.id}>
          <Link
            to={`/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(c.id)}`}
            className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm transition-colors hover:border-accent/50"
          >
            <Badge tone={c.kind === 'should_catch' ? 'accent' : 'neutral'}>
              {c.kind === 'should_catch' ? 'should catch' : 'should not flag'}
            </Badge>
            <span className="font-mono">{c.id}</span>
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
