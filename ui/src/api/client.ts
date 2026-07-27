import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from './schema'

type Schemas = components['schemas']

export type SkillSummary = Schemas['SkillSummary']
export type SkillDetail = Schemas['SkillDetail']
export type CaseDetail = Schemas['CaseDetail']
export type CaseSummary = Schemas['CaseSummary']
export type PendingCase = Schemas['PendingCase']
export type RunRecord = Schemas['RunRecord']
export type RunListItem = Schemas['RunListItem']
export type RunSummary = Schemas['RunSummary']
export type ConsoleConfig = Schemas['ConsoleConfig']
export type GitState = Schemas['GitState']
export type TrialRecord = Schemas['TrialRecord']
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
export type EvalKind = CaseEdits['kind']
export type SkillEdit = Schemas['SkillEdit']
export type PreparedSkill = Schemas['PreparedSkill']
export type StagedSkill = Schemas['StagedSkill']
export type Proposal = Schemas['Proposal']
export type Verdict = Schemas['Verdict']
export type GateRecord = Schemas['GateRecord']
export type Job = Schemas['Job']
export type JobKind = Job['kind']
export type JobState = Job['state']
export type Plan = Schemas['Plan']
export type InboxView = Schemas['InboxView']
export type Attention = Schemas['Attention']
export type NextAction = Schemas['NextAction']
export type ActionKind = NextAction['kind']
export type Signal = Schemas['Signal']
export type WatchState = Schemas['WatchState']
export type Sweep = Schemas['Sweep']
export type ProposeResponse = Schemas['ProposeResponse']
export type DraftResponse = Schemas['DraftResponse']
/** The union of every job kind's request body. Each route validates its own shape server-side. */
export type JobRequest = {
  skill_id: string
  trials?: number | null
  sample?: number | null
  /** eval only: what to score — the working tree, the guidance draft, or the promoted case batch. */
  scope?: 'working' | 'draft' | 'batch'
  targeted?: string[]
  instruction?: string
  stale_ok?: boolean
  repo?: string
  diff?: string
  mr?: number
  project?: string
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
  git: ['git'] as const,
  skills: ['skills'] as const,
  skill: (id: string) => ['skill', id] as const,
  case: (skillId: string, caseId: string) => ['case', skillId, caseId] as const,
  runs: (skillId?: string) => ['runs', skillId ?? 'all'] as const,
  run: (id: string) => ['run', id] as const,
  candidates: ['candidates'] as const,
  batch: ['batch'] as const,
  proposal: (id: string) => ['proposal', id] as const,
  reviews: (skillId?: string) => ['reviews', skillId ?? 'all'] as const,
  review: (id: string) => ['review', id] as const,
  inbox: ['inbox'] as const,
  jobs: ['jobs'] as const,
  job: (id: string) => ['job', id] as const,
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

export function useSkills() {
  return useQuery({ queryKey: keys.skills, queryFn: () => get<SkillSummary[]>('/api/skills') })
}

export function useSkill(id: string) {
  return useQuery({
    queryKey: keys.skill(id),
    queryFn: () => get<SkillDetail>(`/api/skills/${encodeURIComponent(id)}`),
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

export function usePropose() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (branch: string) =>
      // The generated type, not a hand-written subset: `merge_request_url` existed on the wire
      // for months while this inline shape hid it from every caller.
      send<ProposeResponse>('POST', '/api/git/propose', { branch }),
    onSuccess: () => {
      invalidateTriage(client)
      // A guidance branch can be pushed from here too, and its proposal state changes.
      void client.invalidateQueries({ queryKey: ['proposal'] })
    },
  })
}

function invalidateTriage(client: ReturnType<typeof useQueryClient>) {
  void client.invalidateQueries({ queryKey: keys.candidates })
  void client.invalidateQueries({ queryKey: keys.batch })
  // A promotion adds an eval case, so skill listings are stale too.
  void client.invalidateQueries({ queryKey: keys.skills })
}

// --- authoring ------------------------------------------------------------------

/** What is staged for a skill, and whether C6 will let it be published. */
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
    mutationFn: ({
      skillId,
      edit,
      expectHead,
    }: {
      skillId: string
      edit: SkillEdit
      expectHead?: string | null
    }) =>
      send<StagedSkill>('PUT', `/api/skills/${encodeURIComponent(skillId)}/guidance`, {
        edit,
        expect_head: expectHead ?? null,
      }),
    onSuccess: (staged) => invalidateSkill(client, staged.prepared.skill_id),
  })
}

export function useSaveMeta() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({
      skillId,
      metaYaml,
      expectHead,
    }: {
      skillId: string
      metaYaml: string
      expectHead?: string | null
    }) =>
      send<StagedSkill>('PUT', `/api/skills/${encodeURIComponent(skillId)}/meta`, {
        meta_yaml: metaYaml,
        expect_head: expectHead ?? null,
      }),
    onSuccess: (staged) => invalidateSkill(client, staged.prepared.skill_id),
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
