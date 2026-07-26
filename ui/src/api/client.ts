import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from './schema'

type Schemas = components['schemas']

export type SkillSummary = Schemas['SkillSummary']
export type SkillDetail = Schemas['SkillDetail']
export type CaseDetail = Schemas['CaseDetail']
export type CaseSummary = Schemas['CaseSummary']
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
export type PreparedCase = Schemas['PreparedCase']
export type PromoteResponse = Schemas['PromoteResponse']
export type Batch = Schemas['Batch']
export type EvalKind = CaseEdits['kind']
export type SkillEdit = Schemas['SkillEdit']
export type PreparedSkill = Schemas['PreparedSkill']
export type StagedSkill = Schemas['StagedSkill']
export type Proposal = Schemas['Proposal']
export type Verdict = Schemas['Verdict']
export type GateRecord = Schemas['GateRecord']

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
      send<{ branch: string; message: string }>('POST', '/api/git/propose', { branch }),
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
      send<PreparedSkill>(
        'POST',
        `/api/skills/${encodeURIComponent(skillId)}/guidance/preview`,
        { edit },
      ),
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

function invalidateSkill(client: ReturnType<typeof useQueryClient>, skillId: string) {
  void client.invalidateQueries({ queryKey: keys.proposal(skillId) })
  void client.invalidateQueries({ queryKey: keys.skill(skillId) })
  void client.invalidateQueries({ queryKey: keys.skills })
  void client.invalidateQueries({ queryKey: keys.git })
}
