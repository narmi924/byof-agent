export interface CaseRecord {
  case_id: string; factory_id: string; run_id: string; owner_id: string; title: string; state: string; version: number
  created_at: string; updated_at: string; snapshot_id: string | null
  context: Record<string, unknown>; closure: Record<string, unknown> | null; error_code: string | null
}
export interface CaseDetail extends CaseRecord {
  history_cursor?: { at: string; id: string } | null
  activity?: { phase: 'QUEUED' | 'ANALYZING' | 'THINKING' | 'READING_FACTS' | 'SOLVING' | 'STOPPED' | 'IDLE'; stop_target: string | null; started_at: string | null }
  inputs: { input_id: string; input_key?: string; kind: string; payload: Record<string, unknown>; created_at: string; available_at: string; turn_id: string | null; cancelled_at?: string | null }[]
  operations: { operation_id: string; action: string; parameters: Record<string, unknown>; snapshot_id: string; state: string; result: Record<string, unknown> | null; created_at: string; reason_summary?: string | null }[]
}
export const taskRoles = ['maintainer', 'warehouse', 'team_lead', 'planner', 'manager'] as const
export type TaskRole = typeof taskRoles[number]
export const taskFields = ['repair_eta', 'remaining_minutes', 'remaining_setup_minutes', 'receipt_eta', 'comment'] as const
export type TaskField = typeof taskFields[number]
export interface InformationResponse { answer: Record<string, string | number>; actor_id: string; actor_role: string; received_at: string; source: 'authenticated_human_information' }
export interface HandoffResponse { outcome: 'HANDED_OFF'; actor_id: string; actor_role: string; received_at: string; source: 'authenticated_human_handoff'; responsibility_summary: string; risk_summary: string; accept_responsibility: true; accept_risks: true; snapshot_hash: string; snapshot_id: string; run_id: string; source_revision: string; accepted_case_version: number; context_hash: string }
export interface ReviewResponse { outcome: 'APPROVED' | 'REJECTED' | 'STALE'; reason: string | null; candidate_id: string; approval_ids: string[]; source: 'verified_approval_records'; snapshot_hash: string; received_at: string }
export type TaskResponse = InformationResponse | HandoffResponse | ReviewResponse | { outcome: 'HANDED_OFF'; handoff_task_id: string }
export type ReviewOutcome = 'PENDING' | 'APPROVED' | 'REJECTED' | 'STALE' | 'HANDED_OFF'
export interface HumanTask {
  task_id: string; factory_id: string; case_id: string; version: number; question: string; subject_id: string
  owner_id: string | null; owner_role: TaskRole; fields: TaskField[]; state: string
  task_type?: 'INFORMATION' | 'APPROVAL' | 'HANDOFF'; case_version?: number; snapshot_hash?: string | null
  review?: { candidate_id: string; required_scopes: ('publish_plan' | 'allow_overtime')[]; outcome: ReviewOutcome } | null
  response: TaskResponse | null
  created_at: string; updated_at: string; due_at: string; clock: 'real'; reminders_count: number
  send_state: string; delivery_state: string
}

const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const string = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const timestamp = (value: unknown): value is string => string(value) && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))
const nullableString = (value: unknown) => value === null || string(value)
const nullableObject = (value: unknown) => value === null || object(value)

export function parseCase(value: unknown, factoryId: string): CaseRecord {
  if (!object(value) || !['case_id', 'run_id', 'owner_id', 'title', 'state'].every((field) => string(value[field]))
    || value.factory_id !== factoryId || !integer(value.version) || value.version < 1
    || !timestamp(value.created_at) || !timestamp(value.updated_at) || !nullableString(value.snapshot_id)
    || !object(value.context) || !nullableObject(value.closure) || !nullableString(value.error_code)) {
    throw new Error('The case does not match the current factory or its data is incomplete; refresh to check.')
  }
  return value as unknown as CaseRecord
}
export function parseCases(value: unknown, factoryId: string): CaseRecord[] {
  if (!object(value) || !Array.isArray(value.cases)) throw new Error('The case list is not recognized; refresh to check.')
  return value.cases.map((item) => parseCase(item, factoryId))
}
export function parseCaseDetail(value: unknown, factoryId: string, caseId: string): CaseDetail {
  const record = parseCase(value, factoryId)
  if (!object(value) || record.case_id !== caseId || !Array.isArray(value.inputs) || !Array.isArray(value.operations)
    || (value.activity !== undefined && (!object(value.activity)
      || !['QUEUED', 'ANALYZING', 'THINKING', 'READING_FACTS', 'SOLVING', 'STOPPED', 'IDLE'].includes(String(value.activity.phase))
      || !nullableString(value.activity.stop_target) || !(value.activity.started_at === null || timestamp(value.activity.started_at))))
    || !value.inputs.every((item: unknown) => object(item) && string(item.input_id) && (item.input_key === undefined || string(item.input_key)) && string(item.kind) && object(item.payload)
      && timestamp(item.created_at) && timestamp(item.available_at) && nullableString(item.turn_id)
      && (item.cancelled_at === undefined || item.cancelled_at === null || timestamp(item.cancelled_at)))
    || !value.operations.every((item: unknown) => object(item) && string(item.operation_id) && string(item.action) && object(item.parameters)
      && string(item.snapshot_id) && string(item.state) && nullableObject(item.result) && timestamp(item.created_at)
      && (item.reason_summary === undefined || item.reason_summary === null || (string(item.reason_summary) && item.reason_summary.length <= 500)))) {
    throw new Error('The case progress format is incomplete; old records are no longer shown. Refresh to check.')
  }
  if (value.history_cursor !== undefined && value.history_cursor !== null && (!object(value.history_cursor) || !timestamp(value.history_cursor.at) || !string(value.history_cursor.id))) throw new Error('The history page position is not recognized.')
  return value as unknown as CaseDetail
}
export function parseHumanTask(value: unknown, factoryId: string): HumanTask {
  if (!object(value) || !['task_id', 'case_id', 'question', 'subject_id', 'state', 'send_state', 'delivery_state'].every((field) => string(value[field]))
    || value.factory_id !== factoryId || !integer(value.version) || value.version < 1
    || !nullableString(value.owner_id) || !taskRoles.some((role) => role === value.owner_role)
    || !Array.isArray(value.fields) || !value.fields.length || !value.fields.every((field) => taskFields.some((known) => known === field))
    || new Set(value.fields).size !== value.fields.length || value.clock !== 'real' || !integer(value.reminders_count)
    || !['created_at', 'updated_at', 'due_at'].every((field) => timestamp(value[field]))) {
    throw new Error('The task does not match the current factory or its data is incomplete; refresh to check.')
  }
  const response = value.response
  if (value.task_type !== undefined && (!['INFORMATION', 'APPROVAL', 'HANDOFF'].includes(String(value.task_type)) || !integer(value.case_version) || value.case_version < 1 || !nullableString(value.snapshot_hash)
    || (value.task_type === 'APPROVAL' ? !object(value.review) || !string(value.review.candidate_id) || !Array.isArray(value.review.required_scopes) || !value.review.required_scopes.includes('publish_plan') || !value.review.required_scopes.every((scope) => ['publish_plan', 'allow_overtime'].includes(String(scope))) || new Set(value.review.required_scopes).size !== value.review.required_scopes.length || !['PENDING', 'APPROVED', 'REJECTED', 'STALE', 'HANDED_OFF'].includes(String(value.review.outcome)) : value.review !== null))) throw new Error('The task type, review basis or case version is incomplete; refresh the task.')
  if (!taskResponseValid(response, String(value.task_type ?? 'INFORMATION'), String(value.state))) {
    throw new Error('The fields or source of the answer could not be confirmed; refresh to check.')
  }
  return value as unknown as HumanTask
}
function taskResponseValid(response: unknown, kind: string, state: string): boolean {
  if (response === null) return true
  if (!object(response)) return false
  if (response.source === 'authenticated_human_information') return kind === 'INFORMATION' && object(response.answer) && string(response.actor_id) && string(response.actor_role) && timestamp(response.received_at)
    && Object.entries(response.answer).every(([field, answer]) => taskFields.some((known) => known === field) && (field.endsWith('_minutes') ? integer(answer) : field.endsWith('_eta') ? timestamp(answer) : string(answer)))
  if (response.source === 'authenticated_human_handoff') return kind === 'HANDOFF' && state === 'ACCEPTED' && response.outcome === 'HANDED_OFF' && ['actor_id', 'snapshot_hash', 'snapshot_id', 'run_id', 'source_revision', 'context_hash'].every((key) => string(response[key])) && ['planner', 'manager'].includes(String(response.actor_role)) && timestamp(response.received_at) && integer(response.accepted_case_version) && response.accepted_case_version > 0 && response.accept_responsibility === true && response.accept_risks === true && ['responsibility_summary', 'risk_summary'].every((key) => string(response[key]) && response[key].trim().length > 0 && response[key].length <= 2000)
  if (response.source === 'verified_approval_records') return kind === 'APPROVAL' && ['APPROVED', 'REJECTED', 'STALE'].includes(String(response.outcome)) && nullableString(response.reason) && string(response.candidate_id) && string(response.snapshot_hash) && timestamp(response.received_at) && Array.isArray(response.approval_ids) && response.approval_ids.every(string)
  return state === 'CANCELLED' && response.outcome === 'HANDED_OFF' && string(response.handoff_task_id)
}
export function parseHumanTasks(value: unknown, factoryId: string): HumanTask[] {
  if (!object(value) || !Array.isArray(value.tasks)) throw new Error('The task list is not recognized; refresh to check.')
  return value.tasks.map((item) => parseHumanTask(item, factoryId))
}
