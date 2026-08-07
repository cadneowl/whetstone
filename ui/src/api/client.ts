import { useEffect, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from './schema'

type Schemas = components['schemas']

export type SkillSummary = Schemas['SkillSummary']
export type RotStatus = Schemas['RotStatus']
export type SkillDetail = Schemas['SkillDetail']
export type CaseDetail = Schemas['CaseDetail']
export type CaseSummary = Schemas['CaseSummary']
export type PendingCase = Schemas['PendingCase']
/** What a skill's `sidecar:` role resolves to right now — the source tree, its notes, its drift. */
export type SidecarStatus = Schemas['SidecarStatus']
/** Everything the claim ledger knows about one `.agents/` claim, across runs and sweeps. */
export type ClaimHistory = Schemas['ClaimHistory']
/** One query over a source tree's `.agents/` notes, and what the whole graph holds. */
export type SidecarGraphView = Schemas['SidecarGraphView']
/** A folder, a claim, a rule, a citation, a file — or a link that resolves to none of them. */
export type GraphNode = Schemas['Node']
export type GraphEdge = Schemas['Edge']
export type GraphNodeKind = GraphNode['kind']
export type GraphEdgeKind = GraphEdge['kind']
/** One `.agents/` file verbatim, for the panel behind a claim in the graph. */
export type SidecarFile = Schemas['SidecarFile']
/** A search over a skill's own guidance — SKILL.md, its companion pages and its wiki. */
export type GuidanceSearchResult = Schemas['GuidanceSearchResult']
export type GuidanceChunk = Schemas['GuidanceChunk']
export type Contradiction = Schemas['Contradiction']
export type RunRecord = Schemas['RunRecord']
export type RunListItem = Schemas['RunListItem']
export type RunSummary = Schemas['RunSummary']
/** Why a run or gate landed where it did — see `whetstone.explain`. */
export type Explanation = Schemas['Explanation']
export type ConsoleConfig = Schemas['ConsoleConfig']
export type BackendInfo = Schemas['BackendInfo']
export type ModelChoice = Schemas['ModelChoice']
export type GitState = Schemas['GitState']
export type TrialRecord = Schemas['TrialRecord']
export type Dispute = Schemas['Dispute']
export type DisputeRequest = Schemas['DisputeRequest']
export type JudgeView = Schemas['JudgeView']
export type SkillHealth = Schemas['SkillHealth']
export type Retirement = Schemas['Retirement']
export type SimilarCase = Schemas['SimilarCase']
export type TierResult = Schemas['TierResult']
export type DriftSection = Schemas['DriftSection']
export type DriftReport = Schemas['DriftReport']
export type UncoveredMr = Schemas['UncoveredMr']
export type IndexSection = Schemas['IndexSection']
export type PrecedentRef = Schemas['PrecedentRef']
export type CadenceSection = Schemas['CadenceSection']
export type CadenceClock = Schemas['CadenceClock']
export type CadenceMarked = Schemas['CadenceMarked']
export type DeadRule = Schemas['DeadRule']
export type CaseTier = CaseSummary['tier']
export type HoldoutReport = Schemas['HoldoutReport']
export type CaseRun = Schemas['CaseRun']
export type ExpectationOutcome = Schemas['ExpectationOutcome']
export type Finding = Schemas['Finding']
export type Outcome = ExpectationOutcome['outcome']
export type Queue = Schemas['Queue']
export type QueueItem = Schemas['QueueItem']
export type CaseEdits = Schemas['CaseEdits']
export type CandidateCase = Schemas['CandidateCase']
export type Discussion = Schemas['Discussion']
export type ReviewRecord = Schemas['ReviewRecord']
export type ReviewSummary = Schemas['ReviewSummary']
export type ReviewListItem = Schemas['ReviewListItem']
export type ReviewDetail = Schemas['ReviewDetail']
export type FindingVerdict = Schemas['FindingVerdict']
export type PreparedCase = Schemas['PreparedCase']
export type PromoteResponse = Schemas['PromoteResponse']
export type Batch = Schemas['BatchView']
export type PromotedCase = Schemas['PromotedCase']
export type CaseEditRequest = Schemas['CaseEditRequest']
export type CaseWriteResult = Schemas['CaseWriteResult']
export type GraduateResult = Schemas['GraduateResult']
export type EvalKind = CaseEdits['kind']
export type SkillEdit = Schemas['SkillEdit']
export type PreparedSkill = Schemas['PreparedSkill']
export type SavedSkill = Schemas['SavedSkill']
export type Proposal = Schemas['Proposal']
export type Verdict = Schemas['Verdict']
export type GateRecord = Schemas['GateRecord']
export type Job = Schemas['Job']
export type JobKind = Job['kind']
export type JobState = Job['state']
export type Plan = Schemas['Plan']
export type ImprovePrompt = Schemas['ImprovePrompt']
export type PromptVariable = Schemas['PromptVariable']
export type InboxView = Schemas['InboxView']
export type Attention = Schemas['Attention']
export type NextAction = Schemas['NextAction']
export type ActionKind = NextAction['kind']
export type Signal = Schemas['Signal']
export type WatchState = Schemas['WatchState']
export type Sweep = Schemas['Sweep']
export type DraftResponse = Schemas['DraftResponse']
export type SharpeningReport = Schemas['SharpeningReport']
export type TrendPoint = Schemas['TrendPoint']
export type TaskTrendPoint = Schemas['TaskTrendPoint']
export type ProvenFix = Schemas['ProvenFix']
export type TaskView = Schemas['TaskView']
export type TaskCaseSummary = Schemas['TaskCaseSummary']
export type TaskRunRecord = Schemas['TaskRunRecord']
/** The union of every job kind's request body. Each route validates its own shape server-side. */
export type JobRequest = {
  /** Absent only for judge-eval, which measures the deployment-wide judge rather than a skill. */
  skill_id?: string
  trials?: number | null
  sample?: number | null
  /** eval only: what to score — the working tree, the guidance draft, or the promoted cases. */
  scope?: 'working' | 'draft' | 'promoted'
  /**
   * Backend for this one launch, overriding the console default. Empty/absent = the header pick.
   * A known provider only (never a base URL); the server resolves and validates it. Applies to
   * every LLM step — eval, gate, improve, review.
   */
  provider?: string
  model?: string
  targeted?: string[]
  instruction?: string
  stale_ok?: boolean
  /** improve only: draft from this run's failures (e.g. a batch score), not the newest run. */
  run_id?: string | null
  repo?: string
  diff?: string
  /** review only: a merge-request URL (pasted from the browser) or a bare number. */
  mr?: string
  project?: string
  /** synthesize only: which generator, and optionally which parent cases. */
  mode?: 'counterfactual' | 'mutation'
  /**
   * A case-id subset. improve: draft from just these. eval + `scope: 'promoted'`: score just
   * these promoted cases instead of the whole set. synthesize: which parent cases to mutate.
   * Empty/absent means the step's default (for eval, every promoted case).
   */
  cases?: string[]
  /**
   * eval + `scope: 'promoted'` only: score the graduated eval corpus underneath the promoted
   * cases as well. Off by default, because the two questions cost wildly different amounts —
   * "does it catch these two yet?" must not become a thousand-case run just because the skill has
   * a thousand graduated cases. The regression cover is not lost by leaving it off: the gate
   * scores the whole corpus on both sides before a propose is possible.
   */
  with_corpus?: boolean
  /**
   * gate only: measure the baseline again even though an identical measurement is on record.
   *
   * The reuse it overrides is sound by construction — the key covers the commit, the case set, the
   * judge, the reviewer and the model, so a matching record measured the same thing with the same
   * instrument. This exists for the one input that key cannot see: a provider changing the model
   * behind a name.
   */
  fresh_baseline?: boolean
  /** task-eval only: keep each case's workspace on disk instead of a temp dir. */
  keep_workspaces?: boolean
  /** task-gate only: how far the mean score may fall before the gate fails. */
  tolerance?: number
}

/** The shape the API returns for a handled failure — see `ui/errors.py`. */
export interface ApiProblem {
  message: string
  path?: string
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly problem: ApiProblem,
  ) {
    super(problem.message)
  }
}

/**
 * Requests are bounded. The console talks to a local process, so a request that has not answered
 * within this window is wedged, not slow — and an unbounded fetch shows a spinner forever with no
 * way to tell the difference.
 */
const TIMEOUT_MS = 30_000

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { Accept: 'application/json', ...init.headers },
      signal: AbortSignal.timeout(TIMEOUT_MS),
    })
  } catch (cause) {
    const timedOut = cause instanceof DOMException && cause.name === 'TimeoutError'
    throw new ApiError(0, {
      message: timedOut
        ? `the console did not respond within ${TIMEOUT_MS / 1000}s — is \`whetstone ui\` still running?`
        : 'could not reach the console',
    })
  }
  if (!response.ok) {
    // Surface the server's message rather than a generic status: the API is careful to explain
    // itself (missing diff file, invalid skill id), and swallowing that would waste the effort.
    const problem = (await response.json().catch(() => null)) as ApiProblem | null
    throw new ApiError(response.status, problem ?? { message: response.statusText })
  }
  return (await response.json()) as T
}

async function get<T>(path: string): Promise<T> {
  return request<T>(path)
}

export const keys = {
  config: ['config'] as const,
  model: ['model'] as const,
  git: ['git'] as const,
  skills: ['skills'] as const,
  skill: (id: string) => ['skill', id] as const,
  case: (skillId: string, caseId: string) => ['case', skillId, caseId] as const,
  sharpening: (id: string) => ['sharpening', id] as const,
  claims: (id: string) => ['claims', id] as const,
  // The query is in the key: it is a server-side traversal, so two queries are two answers and a
  // cache keyed on the skill alone would show the previous question's picture while typing.
  sidecarGraph: (id: string, q: string, hops: number) => ['sidecar-graph', id, q, hops] as const,
  sidecarFile: (id: string, path: string) => ['sidecar-file', id, path] as const,
  guidanceSearch: (id: string, q: string) => ['guidance-search', id, q] as const,
  tasks: (id: string) => ['tasks', id] as const,
  runs: (skillId?: string) => ['runs', skillId ?? 'all'] as const,
  run: (id: string) => ['run', id] as const,
  disputes: (runId: string) => ['disputes', runId] as const,
  judge: ['judge'] as const,
  health: (skillId: string) => ['health', skillId] as const,
  candidates: ['candidates'] as const,
  batch: ['batch'] as const,
  proposal: (id: string) => ['proposal', id] as const,
  reviews: (skillId?: string) => ['reviews', skillId ?? 'all'] as const,
  review: (id: string) => ['review', id] as const,
  inbox: ['inbox'] as const,
  watch: ['watch'] as const,
  jobs: ['jobs'] as const,
  job: (id: string) => ['job', id] as const,
  // Keyed on the launch it describes, not on the skill: change the run, the selection or the steer
  // and it is a different prompt, so a cached one under the same key would be the wrong answer to
  // the question this view exists to answer.
  improvePrompt: (request: JobRequest) =>
    [
      'improve-prompt',
      request.skill_id ?? '',
      request.run_id ?? '',
      [...(request.cases ?? [])].sort().join(','),
      request.instruction ?? '',
    ] as const,
}

export function useConsoleConfig() {
  return useQuery({
    queryKey: keys.config,
    queryFn: () => get<ConsoleConfig>('/api/config'),
    staleTime: Infinity,
  })
}

export function useGitStatus() {
  return useQuery({ queryKey: keys.git, queryFn: () => get<GitState>('/api/git/status') })
}

/** The model everything the console launches resolves to right now — reviews, runs, gates, drafts. */
export function useModelChoice() {
  return useQuery({ queryKey: keys.model, queryFn: () => get<ModelChoice>('/api/config/model') })
}

/**
 * Change that model for the server's lifetime. The provider must be one the backend knows; an
 * empty provider clears the override back to the configured default. Plans quote the resolved
 * backend, so every launch banner is invalidated on success.
 */
export function useSetModel() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: { provider: string; model: string }) =>
      send<ModelChoice>('PUT', '/api/config/model', body),
    onSuccess: (choice) => {
      client.setQueryData(keys.model, choice)
    },
  })
}

/**
 * Every skill, for the screens that let you pick one.
 *
 * `enabled` exists for the forms that already know their subject: the review form on a skill's own
 * tab renders no picker, and fetching the registry to populate one it will not draw costs a load of
 * every skill and a read of the run store, on a page that asked for one skill.
 */
export function useSkills(enabled = true) {
  return useQuery({
    queryKey: keys.skills,
    queryFn: () => get<SkillSummary[]>('/api/skills'),
    enabled,
  })
}

export function useSkill(id: string) {
  return useQuery({
    queryKey: keys.skill(id),
    queryFn: () => get<SkillDetail>(`/api/skills/${encodeURIComponent(id)}`),
  })
}

/**
 * Whether this skill is getting sharper, and what that claim rests on.
 *
 * Two answers of very different strength — see `whetstone/sharpening.py`. Read the ledger, not the
 * line: the trend moves whenever the corpus, the judge or the model moves, and the healthy loop
 * moves the corpus every week.
 */
/**
 * What runs and sweeps have said about this role's `.agents/` claims — disputed first.
 *
 * `enabled` so the ledger is read when someone opens the list, not on every visit to the skill
 * page: the panel above it already carries the count, which is what the page needs to decide
 * whether there is anything worth opening.
 */
export function useSkillClaims(skillId: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.claims(skillId),
    queryFn: () => get<ClaimHistory[]>(`/api/skills/${encodeURIComponent(skillId)}/claims`),
    enabled,
  })
}

/**
 * The source tree's `.agents/` notes as a graph, filtered by `q`.
 *
 * `enabled` for the same reason the ledger has it: the walk is cheap on a cached tree and is still
 * a filesystem crawl of somebody's monorepo, and it should happen when a person opens the graph
 * rather than on every visit to the skill page.
 *
 * `placeholderData` keeps the previous answer on screen while a new query resolves. Without it
 * every keystroke blanks the picture, and a graph that flashes empty between letters reads as a
 * tree that lost its notes.
 */
export function useSidecarGraph(skillId: string, q: string, hops: number, enabled: boolean) {
  return useQuery({
    queryKey: keys.sidecarGraph(skillId, q, hops),
    queryFn: () =>
      get<SidecarGraphView>(
        `/api/skills/${encodeURIComponent(skillId)}/sidecars/graph` +
          `?q=${encodeURIComponent(q)}&hops=${hops}`,
      ),
    enabled,
    placeholderData: (previous) => previous,
  })
}

/**
 * One `.agents/` file, for the panel behind a claim.
 *
 * `staleTime: Infinity` for the life of the page: the graph's digest already changes when a note
 * changes, and re-reading a file every time someone reopens the same claim would put a filesystem
 * read behind a disclosure triangle.
 */
export function useSidecarFile(skillId: string, path: string | null) {
  return useQuery({
    queryKey: keys.sidecarFile(skillId, path ?? ''),
    queryFn: () =>
      get<SidecarFile>(
        `/api/skills/${encodeURIComponent(skillId)}/sidecars/file?path=${encodeURIComponent(path ?? '')}`,
      ),
    enabled: !!path,
    staleTime: Infinity,
  })
}

/**
 * Search this skill's own guidance — the body, its companion pages and its wiki.
 *
 * Disabled on an empty query rather than fetching everything: with no query the Guidance tab below
 * already shows the whole folder, which is the same answer rendered better.
 */
export function useGuidanceSearch(skillId: string, q: string) {
  return useQuery({
    queryKey: keys.guidanceSearch(skillId, q),
    queryFn: () =>
      get<GuidanceSearchResult>(
        `/api/skills/${encodeURIComponent(skillId)}/guidance/search?q=${encodeURIComponent(q)}`,
      ),
    enabled: q.trim().length > 0,
    placeholderData: (previous) => previous,
  })
}

export function useSharpening(skillId: string) {
  return useQuery({
    queryKey: keys.sharpening(skillId),
    queryFn: () => get<SharpeningReport>(`/api/skills/${encodeURIComponent(skillId)}/sharpening`),
  })
}

/**
 * The task cases a skill carries, its instruments, and its run history.
 *
 * Safe to ask of any skill: a review skill answers `is_task: false` with everything else empty, so
 * the Tasks tab can be rendered conditionally without a second round trip to find out.
 */
export function useTasks(skillId: string) {
  return useQuery({
    queryKey: keys.tasks(skillId),
    queryFn: () => get<TaskView>(`/api/skills/${encodeURIComponent(skillId)}/tasks`),
  })
}

export function useCase(skillId: string, caseId: string) {
  return useQuery({
    queryKey: keys.case(skillId, caseId),
    queryFn: () =>
      get<CaseDetail>(
        `/api/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(caseId)}`,
      ),
  })
}

export function useRuns(skillId?: string) {
  const query = skillId ? `?skill_id=${encodeURIComponent(skillId)}` : ''
  return useQuery({
    queryKey: keys.runs(skillId),
    queryFn: () => get<RunListItem[]>(`/api/runs${query}`),
  })
}

export function useRun(id: string) {
  return useQuery({
    queryKey: keys.run(id),
    queryFn: () => get<RunRecord>(`/api/runs/${encodeURIComponent(id)}`),
  })
}

/**
 * Why this run came out where it did — a reading of the record, not part of it.
 *
 * A separate query from `useRun` because it is a separate resource on the server, for the reason
 * given there: the record is evidence and is frozen on disk, while the reading of it improves.
 */
export function useRunSummary(id: string) {
  return useQuery({
    queryKey: [...keys.run(id), 'summary'],
    queryFn: () => get<Explanation>(`/api/runs/${encodeURIComponent(id)}/summary`),
  })
}

/** The judge every score is computed with: its doctrine, identity, and accumulated evidence. */
export function useJudge() {
  return useQuery({ queryKey: keys.judge, queryFn: () => get<JudgeView>('/api/judge') })
}

/** One skill's state of affairs on one payload — scores, corpus composition, instrument, ground truth. */
export function useHealth(skillId: string) {
  return useQuery({
    queryKey: keys.health(skillId),
    queryFn: () => get<SkillHealth>(`/api/skills/${encodeURIComponent(skillId)}/health`),
  })
}

/**
 * Record that the guidance distill pass ran — the one hand-marked cadence clock. A distill is an
 * ordinary improve run with a consolidating instruction, so nothing in its record distinguishes
 * it; the operator says when one happened. The derived clocks reset from their own stores.
 */
export function useMarkDistill(skillId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () =>
      send<CadenceMarked>('POST', `/api/skills/${encodeURIComponent(skillId)}/cadence/distill`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.health(skillId) })
      void client.invalidateQueries({ queryKey: keys.inbox })
    },
  })
}

/**
 * Flip one eval case between active and archive. The flip is a commit on the skill's staging
 * branch — never a disk write — and C6 requires a fresh gate before the changed corpus ships.
 */
export function useSetTier(skillId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ caseId, tier }: { caseId: string; tier: CaseTier }) =>
      send<TierResult>(
        'POST',
        `/api/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(caseId)}/tier`,
        { tier },
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.health(skillId) })
      void client.invalidateQueries({ queryKey: keys.skill(skillId) })
      void client.invalidateQueries({ queryKey: keys.proposal(skillId) })
      void client.invalidateQueries({ queryKey: keys.inbox })
    },
  })
}

/**
 * Correct or remove a graduated eval case.
 *
 * Both change `skill_hash`, so both retract the gate verdict — which is why they invalidate the
 * proposal query alongside the case itself.
 */
function invalidateCorpus(client: ReturnType<typeof useQueryClient>, skillId: string) {
  void client.invalidateQueries({ queryKey: keys.skill(skillId) })
  void client.invalidateQueries({ queryKey: keys.health(skillId) })
  void client.invalidateQueries({ queryKey: keys.proposal(skillId) })
  void client.invalidateQueries({ queryKey: keys.inbox })
}

export function useEditCase(skillId: string, caseId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (edit: CaseEditRequest) =>
      send<CaseWriteResult>(
        'PUT',
        `/api/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(caseId)}`,
        edit,
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.case(skillId, caseId) })
      invalidateCorpus(client, skillId)
    },
  })
}

export function useDeleteCase(skillId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (caseId: string) =>
      send<CaseWriteResult>(
        'DELETE',
        `/api/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(caseId)}`,
      ),
    onSuccess: () => invalidateCorpus(client, skillId),
  })
}

/** Graduate a promoted case into the eval corpus (promoted_cases/ → eval_cases/ on disk). */
export function useGraduate(skillId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (caseId: string) =>
      send<GraduateResult>(
        'POST',
        `/api/skills/${encodeURIComponent(skillId)}/cases/${encodeURIComponent(caseId)}/graduate`,
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.skill(skillId) })
      void client.invalidateQueries({ queryKey: keys.health(skillId) })
      void client.invalidateQueries({ queryKey: keys.proposal(skillId) })
      void client.invalidateQueries({ queryKey: keys.batch })
      void client.invalidateQueries({ queryKey: keys.inbox })
    },
  })
}

/** Rulings already made on this run's judge verdicts — what the drill-down badges. */
export function useDisputes(runId: string) {
  return useQuery({
    queryKey: keys.disputes(runId),
    queryFn: () => get<Dispute[]>(`/api/runs/${encodeURIComponent(runId)}/disputes`),
  })
}

/**
 * Rule on one judge verdict: same underlying issue, yes or no. Every ruling — agreeing or not —
 * becomes a labeled pair the judge itself is measured against, which is how the judge's quality
 * bar keeps tracking the disagreements it actually faces instead of a frozen fixture.
 */
export function useDisputeVerdict(runId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (request: DisputeRequest) =>
      send<Dispute>('POST', `/api/runs/${encodeURIComponent(runId)}/disputes`, request),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.disputes(runId) })
    },
  })
}

// --- triage -------------------------------------------------------------------

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function useQueue() {
  return useQuery({ queryKey: keys.candidates, queryFn: () => get<Queue>('/api/candidates') })
}

export function useBatch() {
  return useQuery({ queryKey: keys.batch, queryFn: () => get<Batch>('/api/candidates/batch') })
}

/**
 * Validates edits server-side without writing, so a bad region or path is reported against the
 * field that caused it while the person is still editing.
 */
export function usePreview() {
  return useMutation({
    mutationFn: ({ id, edits }: { id: string; edits: CaseEdits }) =>
      send<PreparedCase>('POST', `/api/candidates/${encodeURIComponent(id)}/preview`, { edits }),
  })
}

export function usePromote() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ id, edits }: { id: string; edits: CaseEdits }) =>
      send<PromoteResponse>('POST', `/api/candidates/${encodeURIComponent(id)}/promote`, { edits }),
    onSuccess: () => invalidateTriage(client),
  })
}

/**
 * Draft this candidate's expectation from the evidence.
 *
 * Two calls, like every other spend in the console: the plan first, then the draft. Writes nothing
 * either way — the result lands in the form for a person to accept, edit or discard.
 */
export function useDraftPlan() {
  return useMutation({
    mutationFn: ({ id, skillId }: { id: string; skillId: string }) =>
      send<Plan>('POST', `/api/candidates/${encodeURIComponent(id)}/draft/plan`, {
        skill_id: skillId,
      }),
  })
}

export function useDraftSemantic() {
  return useMutation({
    mutationFn: ({ id, skillId }: { id: string; skillId: string }) =>
      send<DraftResponse>('POST', `/api/candidates/${encodeURIComponent(id)}/draft`, {
        skill_id: skillId,
      }),
  })
}

export function useReject() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      send<unknown>('POST', `/api/candidates/${encodeURIComponent(id)}/reject`, { reason }),
    onSuccess: () => invalidateTriage(client),
  })
}

export function useUndoDecision() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      send<unknown>('DELETE', `/api/candidates/${encodeURIComponent(id)}/decision`),
    onSuccess: () => invalidateTriage(client),
  })
}

/**
 * Rewrite a promoted case — its expectation, region, kind or tier.
 *
 * The server re-derives it from the original candidate, so the edit passes the same validation the
 * promotion did rather than being patched onto the YAML on disk.
 */
export function useEditPromoted() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({
      skillId,
      caseId,
      edits,
    }: {
      skillId: string
      caseId: string
      edits: CaseEdits
    }) =>
      send<PromoteResponse>(
        'PUT',
        `/api/candidates/batch/${encodeURIComponent(skillId)}/${encodeURIComponent(caseId)}`,
        { edits },
      ),
    onSuccess: () => invalidateTriage(client),
  })
}

/**
 * Drop a promoted case from the batch.
 *
 * The server also returns the candidate that wrote it to the queue, so this is a genuine undo of
 * the promotion rather than a delete — which is why it invalidates the queue as well.
 */
export function useRemovePromoted() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ skillId, caseId }: { skillId: string; caseId: string }) =>
      send<Batch>(
        'DELETE',
        `/api/candidates/batch/${encodeURIComponent(skillId)}/${encodeURIComponent(caseId)}`,
      ),
    onSuccess: () => invalidateTriage(client),
  })
}

function invalidateTriage(client: ReturnType<typeof useQueryClient>) {
  void client.invalidateQueries({ queryKey: keys.candidates })
  void client.invalidateQueries({ queryKey: keys.batch })
  // A promotion adds an eval case, so skill listings are stale too.
  void client.invalidateQueries({ queryKey: keys.skills })
}

// --- authoring ------------------------------------------------------------------

/** The on-disk guidance for a skill, and whether a passing gate covers it (advisory C6). */
export function useProposal(skillId: string) {
  return useQuery({
    queryKey: keys.proposal(skillId),
    queryFn: () => get<Proposal>(`/api/skills/${encodeURIComponent(skillId)}/proposal`),
  })
}

/** Validate an edit without writing — what the editor calls while someone is still typing. */
export function usePreviewGuidance() {
  return useMutation({
    mutationFn: ({ skillId, edit }: { skillId: string; edit: SkillEdit }) =>
      send<PreparedSkill>('POST', `/api/skills/${encodeURIComponent(skillId)}/guidance/preview`, {
        edit,
      }),
  })
}

export function useSaveGuidance() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ skillId, edit }: { skillId: string; edit: SkillEdit }) =>
      send<SavedSkill>('PUT', `/api/skills/${encodeURIComponent(skillId)}/guidance`, { edit }),
    onSuccess: (saved) => invalidateSkill(client, saved.prepared.skill_id),
  })
}

export function useSaveMeta() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ skillId, metaYaml }: { skillId: string; metaYaml: string }) =>
      send<SavedSkill>('PUT', `/api/skills/${encodeURIComponent(skillId)}/meta`, {
        meta_yaml: metaYaml,
      }),
    onSuccess: (saved) => invalidateSkill(client, saved.prepared.skill_id),
  })
}

// --- live reviews ---------------------------------------------------------------

export function useReviews(skillId?: string) {
  const query = skillId ? `?skill_id=${encodeURIComponent(skillId)}` : ''
  return useQuery({
    queryKey: keys.reviews(skillId),
    queryFn: () => get<ReviewListItem[]>(`/api/reviews${query}`),
  })
}

export function useReview(id: string) {
  return useQuery({
    queryKey: keys.review(id),
    queryFn: () => get<ReviewDetail>(`/api/reviews/${encodeURIComponent(id)}`),
  })
}

/** Mark one finding correct or false. Mints the candidate that holds the skill to the ruling. */
export function useRuleOnFinding(reviewId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ index, correct, note }: { index: number; correct: boolean; note?: string }) =>
      send<{ record: ReviewRecord; candidate: CandidateCase }>(
        'POST',
        `/api/reviews/${encodeURIComponent(reviewId)}/findings/${index}/verdict`,
        { correct, note: note ?? '' },
      ),
    onSuccess: (res) => invalidateReview(client, reviewId, res.record.skill_id),
  })
}

export function useUndoFindingVerdict(reviewId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (index: number) =>
      send<ReviewRecord>(
        'DELETE',
        `/api/reviews/${encodeURIComponent(reviewId)}/findings/${index}/verdict`,
      ),
    onSuccess: (record) => invalidateReview(client, reviewId, record.skill_id),
  })
}

/** Every field but the target is optional; the server fills the rest from the ruled candidate. */
export interface PromoteFindingArgs {
  index: number
  semantic?: string
  rule_id?: string
  case_id?: string
  line_start?: number | null
  line_end?: number | null
  severity_min?: string | null
}

/** A missed case is minted from scratch, so path and expectation are required; the rest optional. */
export interface MissedCaseArgs {
  skill_id: string
  path: string
  semantic: string
  line_start?: number | null
  line_end?: number | null
  rule_id?: string
  severity_min?: string | null
  case_id?: string
}

/**
 * Commit the eval case a ruling minted, straight from the review — no trip through triage.
 *
 * With no overrides it promotes the candidate as-is (a rejection, or a confirmation that carried a
 * note). A bare confirmation comes back 422 asking for `semantic`, because the expectation cannot
 * be the reviewer's own message — that is the console's cue to reveal the description field.
 */
export function usePromoteFinding(reviewId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ index, ...over }: PromoteFindingArgs) =>
      send<PromoteResponse>(
        'POST',
        `/api/reviews/${encodeURIComponent(reviewId)}/findings/${index}/promote`,
        {
          semantic: over.semantic ?? '',
          rule_id: over.rule_id ?? '',
          case_id: over.case_id ?? '',
          line_start: over.line_start ?? null,
          line_end: over.line_end ?? null,
          severity_min: over.severity_min ?? null,
        },
      ),
    onSuccess: (promoted) => {
      // The skill id comes off the promoted case, so the skill's *detail* is invalidated too, not
      // just the index. Promoting adds to `pending_cases`, which is what the improve workspace
      // selects from — and the case just minted links straight there. Without it the workspace can
      // open on a cached detail that has never heard of the case, drop the id as unknown, and
      // silently fall back to selecting everything.
      invalidateReview(client, reviewId, promoted.prepared.skill_id)
      void client.invalidateQueries({ queryKey: keys.skills })
    },
  })
}

/** Turn a place the skill stayed silent into a committed should-catch case. */
export function usePromoteMissed(reviewId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (args: MissedCaseArgs) =>
      send<PromoteResponse>('POST', `/api/reviews/${encodeURIComponent(reviewId)}/missed`, {
        skill_id: args.skill_id,
        path: args.path,
        semantic: args.semantic,
        line_start: args.line_start ?? null,
        line_end: args.line_end ?? null,
        rule_id: args.rule_id ?? '',
        severity_min: args.severity_min ?? null,
        case_id: args.case_id ?? '',
      }),
    onSuccess: (promoted) => {
      // The skill id comes off the promoted case, so the skill's *detail* is invalidated too, not
      // just the index. Promoting adds to `pending_cases`, which is what the improve workspace
      // selects from — and the case just minted links straight there. Without it the workspace can
      // open on a cached detail that has never heard of the case, drop the id as unknown, and
      // silently fall back to selecting everything.
      invalidateReview(client, reviewId, promoted.prepared.skill_id)
      void client.invalidateQueries({ queryKey: keys.skills })
    },
  })
}

function invalidateReview(
  client: ReturnType<typeof useQueryClient>,
  reviewId: string,
  skillId = '',
) {
  void client.invalidateQueries({ queryKey: keys.review(reviewId) })
  void client.invalidateQueries({ queryKey: ['reviews'] })
  // A ruling adds to — or removes from — the triage queue.
  void client.invalidateQueries({ queryKey: keys.candidates })
  void client.invalidateQueries({ queryKey: keys.batch })
  // And it settles a finding, which is the number on the skill's Reviews tab and the reason the
  // home screen is telling you to go and rule something. Both would otherwise keep asking for a
  // verdict that has just been given.
  void client.invalidateQueries({ queryKey: keys.inbox })
  if (skillId) void client.invalidateQueries({ queryKey: keys.skill(skillId) })
}

function invalidateSkill(client: ReturnType<typeof useQueryClient>, skillId: string) {
  void client.invalidateQueries({ queryKey: keys.proposal(skillId) })
  void client.invalidateQueries({ queryKey: keys.skill(skillId) })
  void client.invalidateQueries({ queryKey: keys.skills })
  void client.invalidateQueries({ queryKey: keys.git })
}

// --- jobs ---------------------------------------------------------------------

/**
 * Work the console launches: scoring a skill, gating a proposal, drafting a change.
 *
 * Polled rather than streamed. The console talks to a process on the same machine and a run emits
 * roughly one event per case, so a second of latency buys nothing worth an SSE endpoint, an
 * EventSource client and reconnection logic on both sides.
 */
export function useJobs() {
  return useQuery({
    queryKey: keys.jobs,
    queryFn: () => get<Job[]>('/api/jobs'),
    // Only while something is in flight: a console left open on a finished list should be idle.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((j) => j.state === 'running') ? POLL_MS : false,
  })
}

export function useJob(id: string | null) {
  const client = useQueryClient()
  return useQuery({
    queryKey: keys.job(id ?? ''),
    enabled: Boolean(id),
    queryFn: async () => {
      const job = await get<Job>(`/api/jobs/${encodeURIComponent(id!)}`)
      if (job.state !== 'running') onJobSettled(client, job)
      return job
    },
    refetchInterval: (query) => (query.state.data?.state === 'running' ? POLL_MS : false),
  })
}

const POLL_MS = 900

/** What a finished job invalidates — the same reads its result just changed. */
function onJobSettled(client: ReturnType<typeof useQueryClient>, job: Job) {
  void client.invalidateQueries({ queryKey: keys.jobs })
  // Every job kind moves a skill along the pipeline, which is exactly what an inbox row reports.
  void client.invalidateQueries({ queryKey: keys.inbox })
  if (job.kind === 'eval') {
    void client.invalidateQueries({ queryKey: ['runs'] })
    void client.invalidateQueries({ queryKey: keys.skill(job.skill_id) })
    void client.invalidateQueries({ queryKey: keys.skills })
  }
  // A gate verdict is exactly what decides whether Propose is available.
  if (job.kind === 'gate' || job.kind === 'update') invalidateSkill(client, job.skill_id)
  if (job.kind === 'baseline') {
    // A probe changes the discrimination section, every case's baseline verdict, and the inbox's
    // saturation proposals — but never the run list, which deliberately excludes baseline records.
    void client.invalidateQueries({ queryKey: keys.health(job.skill_id) })
    void client.invalidateQueries({ queryKey: ['case', job.skill_id] })
  }
  // A review adds a record, and with it findings waiting for a verdict. That is the reviews list on
  // two screens and the count on the skill's tab strip — which reads off the skill payload, so a
  // review run from that very tab would otherwise leave the tab still naming the old number.
  if (job.kind === 'review') {
    void client.invalidateQueries({ queryKey: ['reviews'] })
    void client.invalidateQueries({ queryKey: keys.skill(job.skill_id) })
  }
  // A drift probe fills the health payload's drift section; the inbox is already invalidated above.
  if (job.kind === 'drift') void client.invalidateQueries({ queryKey: keys.health(job.skill_id) })
  // Synthesis writes candidates into the triage queue.
  if (job.kind === 'synthesize') {
    void client.invalidateQueries({ queryKey: keys.candidates })
    void client.invalidateQueries({ queryKey: keys.batch })
  }
  // An index rebuild stages a content change: the proposal state, the health card, and the skill
  // all read differently the moment it lands.
  if (job.kind === 'index') {
    invalidateSkill(client, job.skill_id)
    void client.invalidateQueries({ queryKey: keys.health(job.skill_id) })
  }
}

export function usePlanJob(kind: JobKind) {
  return useMutation({
    mutationFn: (request: JobRequest) => send<Plan>('POST', `/api/jobs/${kind}/plan`, request),
  })
}

export function useLaunchJob(kind: JobKind) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (request: JobRequest) => send<Job>('POST', `/api/jobs/${kind}`, request),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.jobs }),
  })
}

/**
 * The prompt an improve launch would send, filled in — the drafter's input, before it is spent.
 *
 * Fetched only while the panel is open (`enabled`), because assembling it walks the run and the
 * corpus. It calls no model and writes nothing, so a read-only console shows it too.
 */
export function useImprovePrompt(request: JobRequest, enabled: boolean) {
  return useQuery({
    queryKey: keys.improvePrompt(request),
    enabled,
    queryFn: () => send<ImprovePrompt>('POST', '/api/jobs/improve/prompt', request),
  })
}

export function useCancelJob() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => send<Job>('POST', `/api/jobs/${encodeURIComponent(id)}/cancel`),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.jobs }),
  })
}

// No `useStageProposal` here on purpose. A drafted change lands in the editor and is staged by
// `useSaveGuidance` like any hand edit — one write path, so a machine-written rule cannot skip a
// step a human-written one has to pass. `POST /api/jobs/improve/stage` still exists for scripts.

// --- the inbox ----------------------------------------------------------------

/**
 * What happened since you last looked, and what to do about it.
 *
 * Refetched whenever a job settles, because every job kind changes at least one of the facts a row
 * is derived from — a score, a gate verdict, a staged branch.
 */
export function useInbox() {
  return useQuery({
    queryKey: keys.inbox,
    queryFn: () => get<InboxView>('/api/inbox'),
    refetchInterval: (query) => (query.state.data?.watch.polling ? POLL_MS : false),
  })
}

/**
 * When the watcher last looked, and whether it is looking right now.
 *
 * Polled while a sweep runs, and — this is the part that has to live here rather than in a screen —
 * a sweep that *lands* invalidates the triage queue and the inbox. A sweep is the one thing that
 * rewrites the queue without anyone on this side asking it to, so every screen offering the button
 * would otherwise need to remember to refresh, and the one that forgot would show a stale queue
 * with a green "12 new" beside it.
 *
 * Its own endpoint rather than the inbox payload: polling `/api/inbox` for this would rebuild every
 * skill's row — runs, gates, drift, git — every couple of seconds to read one boolean.
 */
export function useWatch() {
  const client = useQueryClient()
  const query = useQuery({
    queryKey: keys.watch,
    queryFn: () => get<WatchState>('/api/watch'),
    refetchInterval: (q) => (q.state.data?.polling ? WATCH_POLL_MS : false),
  })

  // What the watcher is showing, as one comparable value: `SWEEPING` while one is in flight, the
  // timestamp of the sweep on the counter otherwise, and `''` for a watcher that has never swept.
  // `null` only before the state has loaded at all, and that is the one value never recorded.
  const shown = !query.data
    ? null
    : query.data.polling
      ? SWEEPING
      : (query.data.last_sweep?.at ?? '')
  const seen = useRef<string | null>(null)
  useEffect(() => {
    if (shown === null) return
    // A sweep *landed* — not merely started — since the last time this looked. Both of the odd
    // baselines are deliberate, and both were bugs first:
    //
    //   `''`        a console that has never pulled is exactly the case the button exists for.
    //               Reading "no sweep yet" as nothing-to-compare meant the first pull landed
    //               against a baseline that was never taken, so the queue it had just filled was
    //               never re-read: the screen went on saying nothing had been mined, underneath a
    //               line reporting two new candidates.
    //   `SWEEPING`  a screen opened while the timer is mid-sweep is looking at a queue that
    //               predates it. Leaving the baseline unset until the sweep finished made that
    //               sweep's result the baseline, so the candidates it brought in never appeared.
    if (seen.current !== null && seen.current !== shown && shown !== SWEEPING) {
      void client.invalidateQueries({ queryKey: keys.candidates })
      void client.invalidateQueries({ queryKey: keys.inbox })
    }
    seen.current = shown
  }, [shown, client])

  return query
}

/** Slower than a job's poll: this is a background walk of a forge, not a step someone is watching. */
const WATCH_POLL_MS = 2_000

/** Stands in for a sweep in flight, which has no timestamp of its own yet. No `at` can collide. */
const SWEEPING = 'sweeping'

/**
 * Pull the watched projects now rather than waiting for the interval.
 *
 * Answers as soon as the sweep has *started* — see `check_now` in `watch.py` — so the result comes
 * back through `useWatch`, not from here. Callers need both.
 *
 * `since` is a bare date (`2026-08-01`) and makes it a backfill: it reaches back past every
 * watermark and moves none of them.
 */
export function useCheckNow() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (since?: string) =>
      send<WatchState>('POST', '/api/inbox/check', since ? { since } : undefined),
    onSuccess: (state) => client.setQueryData(keys.watch, state),
  })
}
