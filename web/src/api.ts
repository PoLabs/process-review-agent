// All calls to the backend. Everything goes through /api (proxied to :8000 in dev).

export type Target = 'my_process' | 'safety' | 'maintenance' | 'qc' | 'report'
export type Unit = 'kg' | 'g' | 'L' | 'mL'
export type Severity = 'info' | 'minor' | 'major' | 'critical'
export type ApproverRole = 'supervisor' | 'ehs'
export type Recommendation = 'proceed_to_signoff' | 'revise' | 'reject'

export interface ChemicalLine {
  id: string
  quantity: number | null
  unit: Unit | null
  concentration_pct: number | null
}
export interface Step {
  n: number
  action: string
  equipment: string
  chemicals: string[]
  temp_c: number | null
  pressure_bar: number | null
  duration_min: number | null
}
export interface Submission {
  submission_id: string
  product: string
  submitter: string
  chemicals: ChemicalLine[]
  steps: Step[]
  notes: string
}

export interface ExtractResponse {
  form: Partial<Submission> & Record<string, unknown>
  missing: string[]
  quotes: Record<string, string>
  document_digest: string
  warnings: string[]
}

export interface TargetInfo {
  id: Target
  label: string
  description: string
  questions: string[]
}
export interface Workflow {
  title: string
  description: string
  steps: string[]
}
export interface ExamplesResponse {
  targets: TargetInfo[]
  workflows: Workflow[]
  wont_answer: string[]
}

export interface FlagCitation {
  chunk_id: string
  doc_id: string
  version: string
  quote: string
}
export interface Flag {
  lane: 'safety' | 'maintenance' | 'qc' | 'cross' | 'intake'
  kind: string
  severity: Severity
  subjects: string[]
  title: string
  detail: string
  citations: FlagCitation[]
  source: 'rule' | 'model' | 'verify' | 'intake'
  locked: boolean
}

export interface ReportView {
  report_digest: string
  product: string
  summary: string
  recommendation: Recommendation
  rationale: string
  flags: Flag[]
  lanes: Record<string, string>
  required_approvers: ApproverRole[]
}

export interface Signoff {
  role: string
  decision: string
  approver: string
}
export interface ReviewOutput {
  status: 'approved' | 'not_approved' | 'returned_to_submitter'
  recommendation: Recommendation | null
  required_approvers: ApproverRole[]
  signoffs: Signoff[]
  flags: Flag[]
  summary: string
  lanes: Record<string, string>
  incomplete: string[]
  injection_flagged: boolean
  report_digest?: string
  [k: string]: unknown
}

export interface PendingCall {
  tool_use_id: string
  tool: string
  input: { role?: string; report_digest?: string; [k: string]: unknown }
}
export interface RunStatus {
  id: string
  flow: string
  status: 'running' | 'waiting' | 'done' | 'failed'
  pending: PendingCall[]
  output: ReviewOutput | null
  error: string | null
}
export interface TraceEvent {
  node?: string
  name?: string
  [k: string]: unknown
}

export type RefusalReason =
  | 'harmful_intent' | 'harm_screen_failed' | 'injection' | 'asks_approval' | 'asks_rewrite'
  | 'other_submission' | 'out_of_scope' | 'wrong_target' | 'no_bound_run' | 'no_report_yet' | 'too_long'
export interface QACitation {
  kind: 'chunk' | 'field' | 'flag'
  ref: string
  quote: string
  doc_id?: string | null
  version?: string | null
}
export interface QAOutcome {
  status: 'answered' | 'refused' | 'withheld'
  target: Target
  answer: string
  citations: QACitation[]
  refusal_reason: RefusalReason | null
  suggested_target: Target | null
  examples: string[]
  category: string | null
  ehs_notice: boolean
  injection_flagged: boolean
  guard_violations: string[]
}

const TOKEN = import.meta.env.VITE_API_TOKEN as string | undefined

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (TOKEN && method !== 'GET') headers.set('Authorization', `Bearer ${TOKEN}`)
  const res = await fetch(`/api${path}`, { ...init, headers })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* keep status text */
    }
    // FastAPI's own 404 for a route it doesn't have: the proxy reached an API without /process.
    if (res.status === 404 && detail === 'Not Found' && path.startsWith('/process')) {
      detail =
        'The API at this address has no /process routes: it is probably an older server. ' +
        'Start the current one (make serve), or point the dev proxy at it with API_URL.'
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const getExamples = () => call<ExamplesResponse>('/process/examples')

export function extract(file: File) {
  const fd = new FormData()
  fd.append('file', file)
  return call<ExtractResponse>('/process/extract', { method: 'POST', body: fd })
}

export const startReview = (submission: Submission) =>
  call<{ run_id: string; unknown_ids: string[] }>('/process/review', json({ submission }))

export const getRun = (id: string) => call<RunStatus>(`/runs/${encodeURIComponent(id)}`)
export const getEvents = (id: string, since: number) =>
  call<TraceEvent[]>(`/runs/${encodeURIComponent(id)}/events?since=${since}`)
export const listRuns = () => call<RunStatus[]>('/runs')
export const getReport = (digest: string) =>
  call<ReportView>(`/process/reports/${encodeURIComponent(digest)}`)

export const ask = (run_id: string | null, target: Target, question: string) =>
  call<QAOutcome>('/process/ask', json({ run_id, target, question }))

export const decide = (runId: string, toolUseId: string, decision: 'approved' | 'denied', approver: string) =>
  call<{ ok: boolean }>(
    `/runs/${encodeURIComponent(runId)}/decide`,
    json({ decisions: { [toolUseId]: { decision, approver } } }),
  )
