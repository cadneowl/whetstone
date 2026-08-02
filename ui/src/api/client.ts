import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from './schema'

type Schemas = components['schemas']

export type SkillSummary = Schemas['SkillSummary']
export type RotStatus = Schemas['RotStatus']
export type SkillDetail = Schemas['SkillDetail']
export type CaseDetail = Schemas['CaseDetail']
export type CaseSummary = Schemas['CaseSummary']
export type PendingCase = Schemas['PendingCase']
export type Contradiction = Schemas['Contradiction']
export type RunRecord = Schemas['RunRecord']
export type RunListItem = Schemas['RunListItem']
export type RunSummary = Schemas['RunSummary']
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

export function useSkills() {
  return useQuery({ queryKey: keys.skills, queryFn: () => get<SkillSummary[]>('/api/skills') })
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
    onSuccess: () => invalidateReview(client, reviewId),
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
    onSuccess: () => invalidateReview(client, reviewId),
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
    onSuccess: () => {
      invalidateReview(client, reviewId)
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
    onSuccess: () => {
      invalidateReview(client, reviewId)
      void client.invalidateQueries({ queryKey: keys.skills })
    },
  })
}

function invalidateReview(client: ReturnType<typeof useQueryClient>, reviewId: string) {
  void client.invalidateQueries({ queryKey: keys.review(reviewId) })
  void client.invalidateQueries({ queryKey: ['reviews'] })
  // A ruling adds to — or removes from — the triage queue.
  void client.invalidateQueries({ queryKey: keys.candidates })
  void client.invalidateQueries({ queryKey: keys.batch })
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

/** Sweep the watched projects now rather than waiting for the interval. */
export function useCheckNow() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => send<Sweep>('POST', '/api/inbox/check'),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.inbox })
      void client.invalidateQueries({ queryKey: keys.candidates })
    },
  })
}
