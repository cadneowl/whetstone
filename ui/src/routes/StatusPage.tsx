import { Link } from 'react-router-dom'
import {
  useCheckNow,
  useConsoleConfig,
  useGitStatus,
  useInbox,
  useJudge,
  useModelChoice,
  useSkills,
  type JudgeView,
  type SkillSummary,
  type Sweep,
  type WatchState,
} from '@/api/client'
import { Badge, Empty, ErrorNote, Intro, Loading, Sparkline, score, when } from '@/components/primitives'

/**
 * The fleet's state of affairs, on top — not one skill at a time behind the Skills submenu.
 *
 * "How is everything doing?" used to be answerable only by opening skills one by one and reading the
 * badges on each Health tab, three clicks deep. The signals were all computed already — the rot
 * strip, the judge's accuracy, whether the watcher is even running, which model everything resolves
 * to — just scattered across pages and never summed. This puts them in one eyeline: the aggregate at
 * the top, then one row per skill worst-first, each a link into its own Health tab for the detail.
 *
 * Every number here is read from an endpoint that already existed; the page adds no new backend.
 */
export function StatusPage() {
  const skills = useSkills()
  const inbox = useInbox()
  const judge = useJudge()
  const { data: config } = useConsoleConfig()
  const { data: model } = useModelChoice()
  const { data: git } = useGitStatus()

  if (skills.isLoading) return <Loading />
  if (skills.error) return <ErrorNote error={skills.error} />

  const rows = skills.data ?? []
  const watch = inbox.data?.watch
  // The same count the Inbox home shows, read from the same source, so the two never disagree about
  // how many skills need a person.
  const attention = (inbox.data?.inbox.attention ?? []).filter((a) => a.action.kind !== 'nothing')

  const agg = {
    drift: rows.filter((s) => s.rot.drift_alarm).length,
    saturated: rows.reduce((n, s) => n + s.rot.saturated, 0),
    cadence: rows.reduce((n, s) => n + s.rot.cadence_due, 0),
    dead: rows.reduce((n, s) => n + s.rot.dead_rules, 0),
  }

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-lg font-semibold">Status</h1>
        <Intro>
          The whole deployment in one look — how many skills need a person, what rot the fleet is
          carrying, whether the judge behind every score is still trustworthy, and whether Whetstone
          is watching at all. Each skill row links into its Health tab for the full picture.
        </Intro>
      </header>

      <div className="grid gap-3 sm:grid-cols-2">
        <FleetCard skills={rows} attention={attention.length} agg={agg} />
        <div className="space-y-3">
          <JudgeCard judge={judge.data} loading={judge.isLoading} />
          <WatchCard watch={watch} />
        </div>
      </div>

      <EnvironmentBar
        model={model ? model.resolved_model || model.resolved_backend || 'default' : null}
        modelNote={model?.note ?? ''}
        gitBranch={git?.available ? (git.status?.branch ?? null) : null}
        gitClean={git?.status?.clean ?? true}
        readOnly={Boolean(config?.read_only)}
        practice={Boolean(config?.practice_mode)}
        principal={config?.principal.name ?? ''}
      />

      <section>
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-sm font-medium">Skills</h2>
          {rows.length > 0 && <span className="text-xs text-muted">worst first</span>}
        </div>
        {rows.length === 0 ? (
          <Empty>
            No skills under the configured root. A skill is a folder with a{' '}
            <code className="font-mono">SKILL.md</code>.
          </Empty>
        ) : (
          <ul className="space-y-1.5">
            {rows.map((s) => (
              <SkillRow key={s.id} skill={s} />
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      <h2 className="text-xs tracking-wide text-muted uppercase">{title}</h2>
      <div className="mt-2">{children}</div>
    </section>
  )
}

function FleetCard({
  skills,
  attention,
  agg,
}: {
  skills: SkillSummary[]
  attention: number
  agg: { drift: number; saturated: number; cadence: number; dead: number }
}) {
  const quiet = agg.drift === 0 && agg.saturated === 0 && agg.cadence === 0 && agg.dead === 0
  return (
    <Card title="Fleet">
      <p className="text-2xl font-semibold tabular">
        {skills.length}{' '}
        <span className="text-sm font-normal text-muted">
          skill{skills.length === 1 ? '' : 's'}
        </span>
      </p>
      <p className="mt-0.5 text-sm text-muted">
        {attention === 0
          ? 'nothing needs attention'
          : `${attention} need${attention === 1 ? 's' : ''} attention`}
      </p>
      <div className="mt-3 flex flex-wrap gap-2 border-t border-line pt-3">
        {quiet ? (
          <span className="text-xs text-muted italic">no rot signals lit across the fleet</span>
        ) : (
          <>
            {agg.drift > 0 && (
              <Badge tone="warn" title="Skills whose latest drift probe read past the alarm">
                {agg.drift} drifting
              </Badge>
            )}
            {agg.saturated > 0 && (
              <Badge tone="warn" title="Active catch cases the naked model already passes">
                {agg.saturated} saturated
              </Badge>
            )}
            {agg.cadence > 0 && (
              <Badge tone="warn" title="Overdue routine passes across all skills">
                {agg.cadence} pass{agg.cadence === 1 ? '' : 'es'} due
              </Badge>
            )}
            {agg.dead > 0 && (
              <Badge tone="warn" title="meta.yaml rules the evidence no longer stands behind">
                {agg.dead} dead rule{agg.dead === 1 ? '' : 's'}
              </Badge>
            )}
          </>
        )}
      </div>
    </Card>
  )
}

function JudgeCard({ judge, loading }: { judge: JudgeView | undefined; loading: boolean }) {
  return (
    <Card title="Judge">
      {loading ? (
        <p className="text-sm text-muted italic">loading…</p>
      ) : !judge ? (
        <p className="text-sm text-muted italic">no judge configured</p>
      ) : (
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
          <span>
            {judge.id} v{judge.version}
          </span>
          {judge.builtin && <Badge tone="neutral">built-in</Badge>}
          {judge.measured ? (
            <span className="tabular">
              accuracy{' '}
              <span className={judge.measured.accuracy < judge.bar ? 'text-bad' : 'text-good'}>
                {score(judge.measured.accuracy, 2)}
              </span>{' '}
              <span className="text-xs text-muted">
                vs bar {score(judge.bar, 2)} · {judge.measured.missed} missed /{' '}
                {judge.measured.spurious} spurious
              </span>
            </span>
          ) : (
            <span className="text-xs text-muted italic">
              unmeasured — {judge.pairs_total} labeled pair{judge.pairs_total === 1 ? '' : 's'} waiting
            </span>
          )}
          <Link to="/judge" className="ml-auto text-xs text-muted underline hover:text-accent">
            judge →
          </Link>
        </div>
      )}
    </Card>
  )
}

function WatchCard({ watch }: { watch: WatchState | undefined }) {
  const check = useCheckNow()
  const sweep = check.data ?? watch?.last_sweep
  const busy = check.isPending || Boolean(watch?.polling)
  return (
    <Card title="Watch">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm text-muted">
          {watch?.enabled ? (
            <>Watching every {watch.interval_minutes} min.</>
          ) : (
            <>
              Not watching. <span className="font-mono text-xs">[watch] enabled = true</span> turns
              it on.
            </>
          )}
        </p>
        <button
          type="button"
          disabled={busy}
          onClick={() => check.mutate()}
          className="rounded border border-line px-2 py-0.5 text-xs transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:text-muted"
        >
          {busy ? 'Checking…' : 'Check now'}
        </button>
      </div>
      {check.error ? (
        <div className="mt-2">
          <ErrorNote error={check.error} />
        </div>
      ) : (
        sweep && !busy && <SweepLine sweep={sweep} />
      )}
    </Card>
  )
}

function SweepLine({ sweep }: { sweep: Sweep }) {
  if (sweep.error) {
    return (
      <p className="mt-2 rounded border border-bad/40 bg-bad/5 px-2 py-1 text-xs text-bad">
        Check failed at {when(sweep.at)}: {sweep.error}
      </p>
    )
  }
  const found = sweep.found ?? 0
  return (
    <p className={`mt-2 text-xs ${found > 0 ? 'text-accent' : 'text-muted'}`}>
      Checked {when(sweep.at)} · {found > 0 ? `${found} new` : 'nothing new'}
    </p>
  )
}

function EnvironmentBar({
  model,
  modelNote,
  gitBranch,
  gitClean,
  readOnly,
  practice,
  principal,
}: {
  model: string | null
  modelNote: string
  gitBranch: string | null
  gitClean: boolean
  readOnly: boolean
  practice: boolean
  principal: string
}) {
  return (
    <section className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-lg border border-line bg-surface px-4 py-2.5 text-xs text-muted">
      <span>
        model:{' '}
        <span className="font-mono text-ink" title={modelNote || undefined}>
          {modelNote ? 'unresolved' : (model ?? 'default')}
        </span>
      </span>
      {gitBranch && (
        <span>
          git: <span className="font-mono text-ink">{gitBranch}</span>
          {!gitClean && <span className="ml-1 text-warn" title="uncommitted changes">•</span>}
        </span>
      )}
      {readOnly && (
        <Badge tone="warn" title="Mutating routes are disabled server-side">
          read-only
        </Badge>
      )}
      {practice && (
        <Badge tone="warn" title="Runs use deterministic doubles — no model, no spend">
          practice mode
        </Badge>
      )}
      {principal && <span className="ml-auto">{principal}</span>}
    </section>
  )
}

/** One skill's headline numbers and rot, linking into its Health tab for the rest. */
function SkillRow({ skill }: { skill: SkillSummary }) {
  return (
    <li>
      <Link
        to={`/skills/${encodeURIComponent(skill.id)}?tab=health`}
        className="flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm transition-colors hover:border-accent/50"
      >
        <span className="font-medium">{skill.name || skill.id}</span>
        {skill.latest ? (
          <>
            <span className="tabular text-muted" title="recall">
              R {score(skill.latest.recall, 2)}
            </span>
            <span className="tabular text-muted" title="false-positive rate">
              FP {score(skill.latest.fp_rate, 2)}
            </span>
            {skill.holdout && (
              <span
                className="tabular text-xs text-muted"
                title="holdout recall — the slice the improve loop never sees"
              >
                hold {score(skill.holdout.holdout_recall, 2)}
              </span>
            )}
            <span className="text-accent">
              <Sparkline values={skill.recall_trend} />
            </span>
          </>
        ) : (
          <span className="text-xs text-muted italic">never evaluated</span>
        )}
        <span className="ml-auto flex flex-wrap items-center gap-1.5">
          <RotBadges skill={skill} />
        </span>
      </Link>
    </li>
  )
}

function RotBadges({ skill }: { skill: SkillSummary }) {
  const { rot } = skill
  if (rot.signals === 0) return <span className="text-xs text-good">clear</span>
  return (
    <>
      {rot.drift_alarm && (
        <Badge tone="warn" title="Drift probe read past the alarm">
          drift
        </Badge>
      )}
      {rot.saturated > 0 && (
        <Badge tone="warn" title="Active catch cases the naked model already passes">
          {rot.saturated} sat
        </Badge>
      )}
      {rot.cadence_due > 0 && (
        <Badge tone="warn" title="Overdue routine passes">
          {rot.cadence_due} due
        </Badge>
      )}
      {rot.dead_rules > 0 && (
        <Badge tone="warn" title="Rules the evidence no longer stands behind">
          {rot.dead_rules} dead
        </Badge>
      )}
    </>
  )
}
