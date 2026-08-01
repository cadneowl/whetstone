import { useState } from 'react'
import { useTasks, type TaskCaseSummary, type TaskRunRecord } from '@/api/client'
import { LaunchButton } from '@/components/LaunchButton'
import { Badge, Empty, ErrorNote, Loading, score, when } from '@/components/primitives'

/**
 * Driving a task skill — one that *makes* something rather than reporting on a change.
 *
 * This whole surface used to be an error message telling you to go and use the CLI. A task skill in
 * the console read as a review skill with an empty corpus: "Eval cases (0)", a Run evals button
 * that 422'd, and nothing on the page admitting the skill is scored a completely different way.
 *
 * Everything here goes through the same resolver, executor and verifier `whetstone eval task` uses,
 * so the two cannot disagree about what running this skill means.
 */
export function TasksPanel({ skillId }: { skillId: string }) {
  const { data, isLoading, error } = useTasks(skillId)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [keepWorkspaces, setKeepWorkspaces] = useState(false)

  if (isLoading) return <Loading />
  if (error) return <ErrorNote error={error} />
  if (!data) return <Empty>Not found.</Empty>

  const picked = data.cases.filter((c) => selected.has(c.id))
  const failing = data.cases.filter((c) => c.last_passed === false).map((c) => c.id)
  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <div className="space-y-6">
      {data.problem && (
        <div className="rounded-lg border border-bad/40 bg-bad/5 px-4 py-3 text-sm text-bad">
          {data.problem}
        </div>
      )}

      <section className="flex flex-wrap items-start justify-between gap-4">
        <dl className="flex flex-wrap gap-x-8 gap-y-2 text-sm">
          <div>
            <dt className="text-[11px] tracking-wide text-muted uppercase">Does the work</dt>
            <dd className="font-mono text-xs">{data.executor || '—'}</dd>
          </div>
          <div>
            {/* Named beside the executor deliberately. A task score is uninterpretable without
                both, and the one thing a grading line must never say is that the thing under test
                graded itself. */}
            <dt className="text-[11px] tracking-wide text-muted uppercase">Grades it</dt>
            <dd className="font-mono text-xs">{data.verifier || '—'}</dd>
          </div>
          <div>
            <dt className="text-[11px] tracking-wide text-muted uppercase">Per case</dt>
            <dd className="font-mono text-xs">up to {data.max_calls} model call(s)</dd>
          </div>
        </dl>

        <div className="flex flex-col items-end gap-1.5">
          <div className="flex gap-2">
            <LaunchButton
              key={`eval-${picked.length}-${keepWorkspaces}`}
              kind="task-eval"
              request={{
                skill_id: skillId,
                cases: picked.map((c) => c.id),
                keep_workspaces: keepWorkspaces,
              }}
              label={picked.length ? `Run ${picked.length} selected` : 'Run all tasks'}
              disabled={data.cases.length === 0}
              disabledReason="this skill has no task cases"
            >
              <label className="flex items-center gap-2 text-xs text-muted">
                <input
                  type="checkbox"
                  checked={keepWorkspaces}
                  onChange={(e) => setKeepWorkspaces(e.target.checked)}
                />
                keep each workspace on disk — the work the skill produced is the evidence behind a
                failure, and a temp dir leaves only an exit code
              </label>
            </LaunchButton>
            <LaunchButton
              key={`gate-${picked.length}`}
              kind="task-gate"
              request={{
                skill_id: skillId,
                // Default to the cases that failed last time when nothing is ticked: a gate that
                // names nothing can only ever prove that nothing broke.
                targeted: picked.length ? picked.map((c) => c.id) : failing,
              }}
              label="Gate"
              disabled={data.cases.length === 0}
              disabledReason="this skill has no task cases"
            />
          </div>
          <p className="max-w-xs text-right text-xs text-muted">
            The gate runs both the committed version and what is on disk over the same cases, so a
            difference is the guidance rather than the tooling. Naming cases it should fix is what
            turns "nothing broke" into evidence.
          </p>
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-xs tracking-wide text-muted uppercase">
          Task cases ({data.cases.length})
        </h3>
        {data.cases.length === 0 ? (
          <Empty>
            No task cases. Add one under <code className="font-mono">task_cases/&lt;id&gt;/</code>{' '}
            with a <code className="font-mono">case.yaml</code> and the files it starts from.
          </Empty>
        ) : (
          <ul className="space-y-1.5">
            {data.cases.map((c) => (
              <TaskCaseRow
                key={c.id}
                case_={c}
                checked={selected.has(c.id)}
                onToggle={() => toggle(c.id)}
              />
            ))}
          </ul>
        )}
      </section>

      <section>
        <h3 className="mb-2 text-xs tracking-wide text-muted uppercase">
          History ({data.runs.length})
        </h3>
        {data.runs.length === 0 ? (
          <Empty>Never run. Nothing measures whether this skill's work is any good.</Empty>
        ) : (
          <ul className="space-y-1.5">
            {data.runs.map((run) => (
              <TaskRunRow key={run.id} run={run} />
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

function TaskCaseRow({
  case_,
  checked,
  onToggle,
}: {
  case_: TaskCaseSummary
  checked: boolean
  onToggle: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <li className="rounded-lg border border-line bg-surface px-3 py-2 text-sm">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <input type="checkbox" checked={checked} onChange={onToggle} aria-label={case_.id} />
        <button type="button" onClick={() => setOpen(!open)} className="font-mono hover:text-accent">
          {case_.id}
        </button>
        {case_.last_passed === true && <Badge tone="good">passed</Badge>}
        {case_.last_passed === false && <Badge tone="bad">failed</Badge>}
        {case_.last_passed === null && (
          <Badge tone="neutral" title="No run has scored this case">
            unscored
          </Badge>
        )}
        {case_.tier === 'archive' && <Badge tone="neutral">archived</Badge>}
        <span className="text-muted">{case_.instruction}</span>
        {case_.last_score !== null && (
          <span className="ml-auto tabular text-muted">score {score(case_.last_score, 2)}</span>
        )}
      </div>
      {open && (
        <dl className="mt-2 space-y-1 border-t border-line pt-2 text-xs">
          <div className="flex gap-3">
            <dt className="w-20 shrink-0 text-muted">starts from</dt>
            <dd className="font-mono">{case_.files.join(', ') || 'an empty workspace'}</dd>
          </div>
          <div className="flex gap-3">
            <dt className="w-20 shrink-0 text-muted">graded by</dt>
            <dd className="font-mono">{case_.verify || 'the step default'}</dd>
          </div>
          {case_.last_detail && (
            <div className="flex gap-3">
              <dt className="w-20 shrink-0 text-muted">last said</dt>
              <dd>
                <pre className="overflow-x-auto font-mono text-[11px] whitespace-pre-wrap text-muted">
                  {case_.last_detail}
                </pre>
              </dd>
            </div>
          )}
        </dl>
      )}
    </li>
  )
}

function TaskRunRow({ run }: { run: TaskRunRecord }) {
  const scored = run.score.cases ?? []
  const failed = scored.filter((c) => !c.outcome.passed)
  return (
    <li className="rounded-lg border border-line bg-surface px-3 py-2 text-sm">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="text-muted">{when(run.created_at)}</span>
        <span className="tabular">pass {score(run.score.pass_rate, 2)}</span>
        <span className="tabular">mean {score(run.score.mean_score, 2)}</span>
        <span className="text-xs text-muted">{scored.length} case(s)</span>
        {run.score.errors > 0 && (
          <Badge tone="warn" title="Could not be run at all — not the same as failing">
            {run.score.errors} unrunnable
          </Badge>
        )}
        <span className="ml-auto font-mono text-xs text-muted">{run.verifier}</span>
      </div>
      {failed.length > 0 && (
        <p className="mt-1 font-mono text-xs text-bad">
          failed: {failed.map((c) => c.case_id).join(', ')}
        </p>
      )}
      {run.workspaces && (
        <p className="mt-1 font-mono text-[11px] text-muted">workspaces kept: {run.workspaces}</p>
      )}
    </li>
  )
}
