import * as Tabs from '@radix-ui/react-tabs'
import { Link, useParams } from 'react-router-dom'
import { useSkill, type CaseSummary } from '@/api/client'
import { Guidance } from '@/components/Guidance'
import { GuidanceEditor } from '@/components/GuidanceEditor'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Loading, score, when } from '@/components/primitives'

export function SkillDetail() {
  const { skillId = '' } = useParams()
  const { data, isLoading, error } = useSkill(skillId)

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  const { skill, cases, runs } = data

  return (
    <div>
      <header className="mb-5">
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
      </header>

      <Tabs.Root defaultValue="guidance">
        <Tabs.List className="mb-4 flex gap-1 border-b border-line">
          <Trigger value="guidance">Guidance</Trigger>
          <Trigger value="edit">Edit</Trigger>
          <Trigger value="cases">Eval cases ({cases.length})</Trigger>
          <Trigger value="runs">Runs ({runs.length})</Trigger>
          <Trigger value="meta">Metadata</Trigger>
        </Tabs.List>

        <Tabs.Content value="guidance">
          <Guidance detail={data} />
        </Tabs.Content>

        <Tabs.Content value="edit">
          {/* Mounted only while selected, so the draft starts from what is on disk each time the
              tab is opened rather than from a stale copy taken at page load. */}
          <GuidanceEditor detail={data} />
        </Tabs.Content>

        <Tabs.Content value="cases">
          <CaseTable skillId={skill.id} cases={cases} />
        </Tabs.Content>

        <Tabs.Content value="runs">
          <section className="mb-4 rounded-lg border border-line bg-surface/50 p-3">
            <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-sm font-medium">Score this skill</h3>
              <span className="text-xs text-muted">
                {cases.length} eval case{cases.length === 1 ? '' : 's'}
              </span>
            </div>
            {cases.length === 0 ? (
              <p className="text-sm text-muted italic">
                No eval cases to score. Promote some from the triage queue first.
              </p>
            ) : (
              <LaunchButton
                kind="eval"
                request={{ skill_id: skill.id }}
                label="Run evals"
              />
            )}
          </section>

          {runs.length === 0 ? (
            <Empty>Never evaluated. Run the evals above to record one.</Empty>
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
