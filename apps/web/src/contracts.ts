import { parseEffectiveObjective, parseObjectiveContracts } from './preferenceContracts'
import type { EffectiveObjective, ObjectiveContract } from './preferenceContracts'
import { parseValidationCertificates } from './revalidationContracts'
import type { ValidationCertificate } from './revalidationContracts'
import { parseApprovalReviews } from './progressApprovalContracts'
import type { ApprovalReview } from './progressApprovalContracts'
import { parseExecutionSupport } from './executionContracts'
import type { ExecutionSupport } from './executionContracts'
import type { BusinessTerms } from './businessContracts'
import { isBusinessTerms } from './businessContracts'

export interface Factory {
  factory_id: string
  roles: string[]
  last_synced_at: string | null
  snapshot_id: string | null
}

export interface TimeSpan { start_at: string; end_at: string }
/** Gaps between calendar windows are breaks and non-working time; the source has no explicit break objects. */
export interface CalendarWindow extends TimeSpan { kind: 'NORMAL' | 'OVERTIME' }

export interface Product { product_id: string; name: string; batch_size: number; route_version: string }
export interface Material { material_id: string; name: string; unit: string }
export interface RouteStep {
  step_id: string
  product_id: string
  operation_code: string
  name: string
  skill: string
  predecessors: string[]
  route_version?: string
  resource_type?: string
  setup_min?: number
  cycle_sec_per_unit?: number
  quality_gate?: boolean
}
export interface Policy {
  policy_version: string
  progress_revalidation_enabled: boolean
  freeze_window_min?: number
  first_changeover_min?: number
  same_product_changeover_min?: number
  different_product_changeover_min?: number
}
export interface FactoryResource {
  version?: number
  resource_id: string
  resource_type: string
  operation_codes: string[]
  status: 'AVAILABLE' | 'MAINTENANCE' | 'DOWN' | 'UNKNOWN'
  calendar?: CalendarWindow[]
  unavailable?: TimeSpan[]
  last_operation_id?: string | null
  last_product_id?: string | null
}
export interface FactoryWorker {
  version?: number
  worker_id: string
  skills: string[]
  status: 'AVAILABLE' | 'ABSENT' | 'UNKNOWN'
  overtime_available: boolean
  calendar?: CalendarWindow[]
  unavailable?: TimeSpan[]
}
export interface Order {
  order_id: string
  product_id: string
  quantity: number
  due_at: string
  hard_deadline: boolean
  status: string
  split_revision: number
  priority_weight: number
  version: number
}

export interface ProductionBatch {
  batch_id: string; order_id: string; product_id: string; route_version: string; quantity: number; sequence: number
  purpose: 'CUSTOMER' | 'STOCK' | 'CANCELLED'
}

/** Qualified surplus finished goods verified by the server against the same snapshot; excludes WIP, unchecked or failed goods. */
export interface FinishedGoodsLot { batch_id: string; product_id: string; quantity: number; completed_at: string }

export interface Snapshot {
  schema_version?: 'byof.snapshot/1' | 'byof.snapshot/2' | 'byof.snapshot/3'
  snapshot_id: string
  factory_id: string
  run_id?: string
  snapshot_clock: string
  horizon?: TimeSpan
  active_plan_version: string | null
  active_plan_hash?: string | null
  content_hash: string
  source: {
    source_system: string
    observed_at: string
    effective_at: string
    ownership: 'enterprise_fact' | 'simulator_fact'
    freshness: 'CURRENT' | 'STALE' | 'UNKNOWN'
    complete: boolean
    source_revision?: string
  }
  profile: {
    timezone: string
    version: string
    policy?: Policy
    products: Product[]
    materials: Material[]
    routes: RouteStep[]
  }
  orders: Order[]
  inventory: { material_id: string; unit: string; on_hand: number; reserved: number; version: number }[]
  receipts: { receipt_id: string; material_id: string; quantity: number; unit: string; eta: string; status: string; received_at: string | null; version: number }[]
  resources: FactoryResource[]
  workers: FactoryWorker[]
  actuals: ActualExecution[]
  reservations?: Reservation[]
  business_terms?: BusinessTerms | null
  production_batches?: ProductionBatch[] | null
}

export interface ExecutionSegment extends TimeSpan { phase: 'SETUP' | 'PRODUCTION'; source_event_id: string }
export interface ActualExecution {
  operation_id: string
  state: 'SETUP' | 'IN_PROGRESS' | 'COMPLETED' | 'BLOCKED'
  completed_quantity: number
  actual_start: string | null
  actual_end: string | null
  quality_state: string
  changeover_start?: string | null
  segments?: ExecutionSegment[]
  remaining_minutes?: number | null
  remaining_setup_minutes?: number | null
  batch_id?: string
  resource_id?: string
  worker_id?: string
  consumed?: { material_id: string; quantity: number; unit: string; event_id: string }[]
}
export interface Reservation {
  reservation_id: string
  batch_id: string
  material_id: string
  quantity: number
  unit: string
  plan_version: string
  source_event_id: string
  created_at: string
}

export interface Assignment {
  operation_id: string
  resource_id: string
  worker_id: string
  start_at: string
  end_at: string
  changeover_start: string
  resume_at?: string | null
  resume_changeover_start?: string | null
}

export interface Metric { name: string; value: number | null; unit: string; lower_bound: number | null; unknown_reason: string | null }
export interface VersionBinding { snapshot_hash: string; profile_version: string; policy_version: string; objective_version: string; baseline_plan_version: string | null; planning_revision?: number; scope_version?: number }
export interface Candidate {
  schema_version?: 'byof.candidate/1' | 'byof.candidate/2' | 'byof.candidate/3'
  candidate_id: string
  factory_id: string
  version: number
  content_hash: string
  binding: VersionBinding
  native_status: NativeStatus
  solver_passes?: SolverPass[]
  last_search_status?: NativeStatus | null
  has_solution: boolean
  empty_demand?: boolean
  termination_reason: 'COMPLETED' | 'TIME_LIMIT' | 'CANCELLED' | 'WORKER_FAILURE' | 'MODEL_ERROR'
  objective: Metric[]
  proven_objective_levels: number
  assignments: Assignment[]
  scenario: { field: string; value: string | number | boolean | null; reason: string; confirmation_id: string | null }[]
  required_consents: string[]
  checker: { status: 'PASS' | 'FAIL' | 'NOT_RUN'; issues: { code: string; object_id: string | null; message: string }[] }
  effective_not_before: string
  accept_before: string
  new_actions_not_before?: string | null
}

export type NativeStatus = 'OPTIMAL' | 'FEASIBLE' | 'INFEASIBLE' | 'UNKNOWN' | 'MODEL_INVALID'
export interface SolverPass {
  objective_name: string
  native_status: NativeStatus
  has_solution: boolean
  objective_value: number | null
  best_bound: number | null
  wall_time_seconds: number
}

export type ApprovalScope = 'publish_plan' | 'allow_overtime'
export interface Approval {
  approval_id: string
  candidate_hash: string
  approver_id: string
  approver_role: 'planner' | 'manager'
  action_scope: ApprovalScope
  decision: 'APPROVED' | 'REJECTED'
  decided_at: string
  expires_at: string
}
export interface OrderFacts {
  order_id: string; product_id: string; quantity: number; due_at: string; status: string
  qualified_completed_quantity: number; in_progress_quantity: number; plan_covered_quantity: number; uncovered_quantity: number
  planned_completion_at: string | null; planned_ready_today_quantity: number; planned_on_time_quantity: number
  direct_shortage_materials: string[]; material_quantity_upper_bound: number; forecast_requires_revalidation: boolean
  previous_completion_at?: string | null; previous_covered_quantity?: number
}
export interface PlanReview { as_of: string; accept_before: string; orders: OrderFacts[]; overtime: {worker_id: string; resource_id: string; start_at: string; end_at: string; minutes: number}[] }
export interface CandidateRecord {
  review?: PlanReview | null
  run_id?: string | null
  case_id?: string | null
  candidate: Candidate
  snapshot_id: string
  state: 'CANDIDATE' | 'APPROVED' | 'STALE' | 'NO_SOLUTION' | 'CHECK_FAILED'
  approvals: Approval[]
}
export interface Job {
  job_id: string
  state: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  candidate_id: string | null
  error_code: string | null
  created_at: string
  allow_overtime: boolean
  new_actions_not_before?: string | null
}
export interface Release {
  release_id: string
  operation_id: string
  factory_id: string
  candidate_hash: string
  payload_hash: string
  approval_ids: string[]
  local_state: 'LOCAL_COMMITTED'
  source_state: 'PENDING_SOURCE' | 'UNKNOWN' | 'ACTIVE' | 'REJECTED' | 'ACCEPTED_PENDING_EFFECTIVE'
  execution_state: 'NOT_STARTED' | 'IN_PROGRESS' | 'COMPLETED' | 'BLOCKED'
  source_receipt_id: string | null
  committed_at: string
  effective_at: string | null
}
export interface Publication { release: Release; candidate_id: string; error_code: string | null }
export interface SimulatorStatus { factory_id: string; run_id: string; mode: 'PAUSED' | 'RUNNING'; interval_ms: number; business_clock: string; server_time?: string; scenario?: null | { enabled: boolean; seed: number; every_minutes: number; counter: number; next_at: string }; replay?: null | { origin_run_id: string; next_revision: number; target_revision: number; done: boolean; error_code: string | null } }
export interface ReplayRequest { request_id: string; expected_run_id: string }
export interface TodayRunRequest { request_id: string; expected_run_id: string; scenario_version?: 'workshop-full-2' }
export interface SimulatorCommand { request_id: string; run_id: string; kind: 'clock.step' | 'clock.run' | 'clock.pause' | 'resource.down' | 'resource.outage' | 'resource.restore' | 'worker.absent' | 'worker.leave' | 'worker.return' | 'scenario.configure' | 'receipt.receive' | 'receipt.delay' | 'receipt.shortfall' | 'receipt.cancel' | 'receipt.add' | 'inventory.reconcile' | 'execution.confirm_remaining' | 'quality.record' | 'quality.scrap' | 'order.add' | 'order.revise' | 'delivery_rule.set' | 'expedite_quote.set' | 'expedite_quote.remove' | 'overtime_window.set'; payload: Record<string, string | number | boolean | null> }
export type Freshness = 'CURRENT' | 'STALE' | 'UNKNOWN'
export interface Workspace {
  order_facts?: OrderFacts[]
  snapshot: Snapshot | null; finished_goods?: FinishedGoodsLot[] | null; last_synced_at: string | null; server_time?: string; freshness?: Freshness; jobs: Job[]; candidates: CandidateRecord[]; publications?: Publication[]; objective_state?: EffectiveObjective; objective_contracts?: Record<string, ObjectiveContract>; validation_certificates?: ValidationCertificate[]; approval_reviews?: ApprovalReview[]; execution_support?: ExecutionSupport }
export interface AgentRun {
  run_id: string
  state: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'NEEDS_INPUT'
  created_at: string
  result: null | { status: 'OK' | 'NEEDS_INPUT'; summary: string; tool: null | 'query' | 'solve_scenario' | 'clarify'; entity?: string; record_count?: number; snapshot_id?: string; source_revision?: string; job_id?: string }
  error_code: string | null
  model_requests: number
}

type Guard = (value: unknown) => boolean
const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const string: Guard = (value) => typeof value === 'string' && value.length > 0
const number: Guard = (value) => typeof value === 'number' && Number.isSafeInteger(value)
const nonNegative: Guard = (value) => number(value) && (value as number) >= 0
const finite: Guard = (value) => typeof value === 'number' && Number.isFinite(value)
const duration: Guard = (value) => finite(value) && (value as number) >= 0
const boolean: Guard = (value) => typeof value === 'boolean'
const nullable = (guard: Guard): Guard => (value) => value === null || guard(value)
const optional = (guard: Guard): Guard => (value) => value === undefined || guard(value)
const array = (guard: Guard): Guard => (value) => Array.isArray(value) && value.every(guard)
const oneOf = (...values: string[]): Guard => (value) => typeof value === 'string' && values.includes(value)
const shape = (fields: Record<string, Guard>): Guard => (value) => object(value) && Object.entries(fields).every(([name, guard]) => guard(value[name]))
const timestamp: Guard = (value) => typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))
const strings = array(string)
const nativeStatus = oneOf('OPTIMAL', 'FEASIBLE', 'INFEASIBLE', 'UNKNOWN', 'MODEL_INVALID')
const span: Guard = (value) => shape({ start_at: timestamp, end_at: timestamp })(value)
  && Date.parse((value as TimeSpan).start_at) < Date.parse((value as TimeSpan).end_at)
const timeSpans = array(span)
const calendarWindows = array((value) => span(value) && oneOf('NORMAL', 'OVERTIME')((value as CalendarWindow).kind))

const factoryGuard = shape({ factory_id: string, roles: strings, last_synced_at: nullable(timestamp), snapshot_id: nullable(string) })
const snapshotGuard = shape({
  business_terms: optional(nullable(isBusinessTerms)),
  production_batches: optional(nullable(array(shape({ batch_id: string, order_id: string, product_id: string, route_version: string,
    quantity: value => number(value) && (value as number) > 0, sequence: value => number(value) && (value as number) > 0, purpose: oneOf('CUSTOMER', 'STOCK', 'CANCELLED') })))),
  schema_version: optional(oneOf('byof.snapshot/1', 'byof.snapshot/2', 'byof.snapshot/3')),
  snapshot_id: string, factory_id: string, run_id: optional(string), snapshot_clock: timestamp, active_plan_version: nullable(string), active_plan_hash: optional(nullable(string)), content_hash: string,
  horizon: optional(shape({ start_at: timestamp, end_at: timestamp })),
  source: shape({ source_system: string, observed_at: timestamp, effective_at: timestamp, ownership: oneOf('enterprise_fact', 'simulator_fact'), freshness: oneOf('CURRENT', 'STALE', 'UNKNOWN'), complete: boolean, source_revision: optional(string) }),
  profile: shape({
    policy: optional(shape({
      policy_version: string, progress_revalidation_enabled: boolean,
      freeze_window_min: optional(nonNegative), first_changeover_min: optional(nonNegative),
      same_product_changeover_min: optional(nonNegative), different_product_changeover_min: optional(nonNegative),
    })),
    timezone: (value) => { try { if (typeof value !== 'string') return false; new Intl.DateTimeFormat('en-US', { timeZone: value }); return true } catch { return false } },
    version: string,
    products: array(shape({ product_id: string, name: string, batch_size: number, route_version: string })),
    materials: array(shape({ material_id: string, name: string, unit: string })),
    routes: array(shape({
      step_id: string, product_id: string, operation_code: string, name: string, skill: string, predecessors: strings,
      route_version: optional(string), resource_type: optional(string), setup_min: optional(nonNegative),
      cycle_sec_per_unit: optional(nonNegative), quality_gate: optional(boolean),
    })),
  }),
  orders: array(shape({ order_id: string, product_id: string, quantity: number, due_at: timestamp, hard_deadline: boolean, status: string, split_revision: number, priority_weight: number, version: number })),
  inventory: array(shape({ material_id: string, unit: string, on_hand: number, reserved: number, version: number })),
  receipts: array(shape({ receipt_id: string, material_id: string, quantity: number, unit: string, eta: timestamp, status: string, received_at: nullable(timestamp), version: number })),
  resources: array(shape({
    version: optional(value => number(value) && (value as number) > 0),
    resource_id: string, resource_type: string, operation_codes: strings, status: oneOf('AVAILABLE', 'MAINTENANCE', 'DOWN', 'UNKNOWN'),
    calendar: optional(calendarWindows), unavailable: optional(timeSpans),
    last_operation_id: optional(nullable(string)), last_product_id: optional(nullable(string)),
  })),
  workers: array(shape({
    version: optional(value => number(value) && (value as number) > 0),
    worker_id: string, skills: strings, status: oneOf('AVAILABLE', 'ABSENT', 'UNKNOWN'), overtime_available: boolean,
    calendar: optional(calendarWindows), unavailable: optional(timeSpans),
  })),
  actuals: array(shape({
    operation_id: string, state: oneOf('SETUP', 'IN_PROGRESS', 'COMPLETED', 'BLOCKED'), completed_quantity: nonNegative,
    actual_start: nullable(timestamp), actual_end: nullable(timestamp), quality_state: string,
    changeover_start: optional(nullable(timestamp)), remaining_minutes: optional(nullable(nonNegative)), remaining_setup_minutes: optional(nullable(nonNegative)),
    segments: optional(array(shape({ phase: oneOf('SETUP', 'PRODUCTION'), start_at: timestamp, end_at: timestamp, source_event_id: string }))),
    batch_id: optional(string), resource_id: optional(string), worker_id: optional(string),
    consumed: optional(array(shape({ material_id: string, quantity: nonNegative, unit: string, event_id: string }))),
  })),
  reservations: optional(array(shape({ reservation_id: string, batch_id: string, material_id: string, quantity: nonNegative, unit: string, plan_version: string, source_event_id: string, created_at: timestamp }))),
})
const approvalGuard = shape({ approval_id: string, candidate_hash: string, approver_id: string, approver_role: oneOf('planner', 'manager'), action_scope: oneOf('publish_plan', 'allow_overtime'), decision: oneOf('APPROVED', 'REJECTED'), decided_at: timestamp, expires_at: timestamp })
const candidateGuard = shape({
  schema_version: optional(oneOf('byof.candidate/1', 'byof.candidate/2', 'byof.candidate/3')),
  candidate_id: string, factory_id: string, version: number, content_hash: string,
  binding: shape({ snapshot_hash: string, profile_version: string, policy_version: string, objective_version: string, baseline_plan_version: nullable(string), planning_revision: optional(nonNegative), scope_version: optional(nonNegative) }),
  native_status: nativeStatus, has_solution: boolean, empty_demand: optional(boolean),
  solver_passes: optional(array(shape({ objective_name: string, native_status: nativeStatus, has_solution: boolean, empty_demand: optional(boolean), objective_value: nullable(number), best_bound: nullable(finite), wall_time_seconds: duration }))),
  last_search_status: optional(nullable(nativeStatus)),
  termination_reason: oneOf('COMPLETED', 'TIME_LIMIT', 'CANCELLED', 'WORKER_FAILURE', 'MODEL_ERROR'),
  objective: array(shape({ name: string, value: nullable(number), unit: string, lower_bound: nullable(number), unknown_reason: nullable(string) })),
  proven_objective_levels: number,
  assignments: array(shape({ operation_id: string, resource_id: string, worker_id: string, start_at: timestamp, end_at: timestamp, changeover_start: timestamp, resume_at: optional(nullable(timestamp)), resume_changeover_start: optional(nullable(timestamp)) })),
  scenario: array(shape({ field: string, value: (value) => value === null || string(value) || number(value) || boolean(value), reason: string, confirmation_id: nullable(string) })),
  required_consents: strings,
  checker: shape({ status: oneOf('PASS', 'FAIL', 'NOT_RUN'), issues: array(shape({ code: string, object_id: nullable(string), message: string })) }),
  effective_not_before: timestamp, accept_before: timestamp,
  new_actions_not_before: optional(nullable(timestamp)),
})
const releaseGuard = shape({
  release_id: string, operation_id: string, factory_id: string, candidate_hash: string, payload_hash: string,
  approval_ids: (value) => strings(value) && (value as string[]).length > 0, local_state: oneOf('LOCAL_COMMITTED'),
  source_state: oneOf('PENDING_SOURCE', 'UNKNOWN', 'ACTIVE', 'REJECTED', 'ACCEPTED_PENDING_EFFECTIVE'),
  execution_state: oneOf('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'BLOCKED'), source_receipt_id: nullable(string), committed_at: timestamp, effective_at: nullable(timestamp),
})
const orderFactsGuard = shape({
  order_id: string, product_id: string, status: string, quantity: number, due_at: timestamp,
  qualified_completed_quantity: number, in_progress_quantity: number, plan_covered_quantity: number, uncovered_quantity: number,
  planned_completion_at: nullable(timestamp), planned_ready_today_quantity: number, planned_on_time_quantity: number,
  direct_shortage_materials: array(string), material_quantity_upper_bound: number, forecast_requires_revalidation: boolean,
  previous_completion_at: optional(nullable(timestamp)), previous_covered_quantity: optional(number),
})
const planReviewGuard = shape({
  as_of: timestamp, accept_before: timestamp, orders: array(orderFactsGuard),
  overtime: array(shape({worker_id: string, resource_id: string, start_at: timestamp, end_at: timestamp, minutes: number})),
})

const workspaceGuard = shape({
  order_facts: optional(array(orderFactsGuard)),
  snapshot: nullable(snapshotGuard), last_synced_at: nullable(timestamp), server_time: optional(timestamp), freshness: optional(oneOf('CURRENT', 'STALE', 'UNKNOWN')),
  finished_goods: optional(nullable(array(shape({ batch_id: string, product_id: string, quantity: value => number(value) && (value as number) > 0, completed_at: timestamp })))),
  jobs: array(shape({ job_id: string, state: oneOf('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'), candidate_id: nullable(string), error_code: nullable(string), created_at: timestamp, allow_overtime: boolean, new_actions_not_before: optional(nullable(timestamp)) })),
  candidates: array(shape({ review: optional(nullable(planReviewGuard)), candidate: candidateGuard, snapshot_id: string, run_id: optional(nullable(string)), case_id: optional(nullable(string)), state: oneOf('CANDIDATE', 'APPROVED', 'STALE', 'NO_SOLUTION', 'CHECK_FAILED'), approvals: array(approvalGuard) })),
  publications: optional(array(shape({ release: releaseGuard, candidate_id: string, error_code: nullable(string) }))),
})

const agentRunGuard = shape({
  run_id: string, state: oneOf('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'NEEDS_INPUT'), created_at: timestamp,
  result: nullable(shape({ status: oneOf('OK', 'NEEDS_INPUT'), summary: string, tool: nullable(oneOf('query', 'solve_scenario', 'clarify')), entity: optional(string), record_count: optional(number), snapshot_id: optional(string), source_revision: optional(string), job_id: optional(string) })),
  error_code: nullable(string), model_requests: number,
})

export function parseFactories(value: unknown): Factory[] {
  if (!object(value) || !array(factoryGuard)(value.factories)) throw new Error('The factory list is not recognized; ask the administrator to check the connection.')
  return value.factories as Factory[]
}
export function parseWorkspace(value: unknown, factoryId: string): Workspace {
  if (!workspaceGuard(value)) throw new Error('The production data format is incomplete, so updates stopped; sync again or contact the administrator.')
  const workspace = value as Workspace
  if (workspace.candidates.some(({ candidate }) => candidate.empty_demand === true && (candidate.schema_version !== 'byof.candidate/3' || !candidate.has_solution || candidate.assignments.length > 0))) throw new Error('An empty-demand plan must be a valid version 3 plan without operations; reload it.')
  if (workspace.candidates.some(({ candidate }) => candidate.new_actions_not_before != null && (candidate.schema_version !== 'byof.candidate/3' || Date.parse(candidate.new_actions_not_before) % 60_000 !== 0 || Date.parse(candidate.new_actions_not_before) < Date.parse(candidate.effective_not_before)))) throw new Error("The plan's earliest new-action time does not match its version or acceptance time; ask the administrator to check.")
  workspace.execution_support = parseExecutionSupport(workspace.execution_support)
  if (workspace.objective_state !== undefined) parseEffectiveObjective(workspace.objective_state)
  if (workspace.objective_contracts !== undefined) parseObjectiveContracts(workspace.objective_contracts)
  if (workspace.validation_certificates !== undefined) parseValidationCertificates(workspace.validation_certificates, factoryId)
  if (workspace.approval_reviews !== undefined) parseApprovalReviews(workspace.approval_reviews, factoryId)
  if ((workspace.snapshot && workspace.snapshot.factory_id !== factoryId)
    || workspace.candidates.some((record) => record.candidate.factory_id !== factoryId)
    || workspace.publications?.some((record) => record.release.factory_id !== factoryId)) {
    throw new Error('The returned data does not match the current factory and is not shown. Contact the administrator.')
  }
  const metricUnits: Record<string, string> = { weighted_tardiness: 'minutes', incremental_overtime_metric: 'minutes', changed_operations: 'operations', total_start_shift: 'minutes', makespan: 'minutes' }
  if (workspace.candidates.some((record) => record.candidate.objective.some((metric) => metricUnits[metric.name] && metric.unit !== metricUnits[metric.name]))) {
    throw new Error('The unit of a plan metric could not be confirmed and is not shown. Contact the administrator.')
  }
  workspace.publications?.forEach((record) => parseRelease(record.release, factoryId))
  return workspace
}
export function parsePlanExport(value: unknown, factoryId: string, candidateId: string, candidateHash: string): Record<string, unknown> {
  const report = shape({ checker_version: string, snapshot_hash: string, status: oneOf('PASS', 'FAIL', 'NOT_RUN'), issues: array(shape({ code: string, object_id: nullable(string), message: string })) })
  if (!shape({ schema_version: oneOf('byof.plan-export/1'), purpose: oneOf('MANUAL_REVIEW'), factory_id: string, run_id: string,
    candidate: candidateGuard, original_snapshot_id: string, original_snapshot_hash: string, current_snapshot_id: string, current_snapshot_hash: string,
    original_checker: report, current_checker: nullable(report), current_error_code: nullable(string), exported_by: string, exported_at: timestamp, notice: string, content_hash: string,
  })(value) || !object(value)) throw new Error('The export format could not be confirmed; the download did not start. Refresh and try again.')
  const candidate = value.candidate as Candidate
  const original = value.original_checker as { status: string; snapshot_hash: string }
  const current = value.current_checker as { status: string; snapshot_hash: string } | null
  if (value.factory_id !== factoryId || candidate.factory_id !== factoryId || candidate.candidate_id !== candidateId || candidate.content_hash !== candidateHash
    || candidate.binding.snapshot_hash !== value.original_snapshot_hash || original.status !== 'PASS' || original.snapshot_hash !== value.original_snapshot_hash
    || (current !== null && (current.snapshot_hash !== value.current_snapshot_hash || value.current_error_code !== null))
    || (current === null && value.current_error_code === null)) throw new Error('The export does not match the plan or factory facts; the download did not start. Ask the administrator to check.')
  return value
}
export function parseRelease(value: unknown, factoryId: string): Release {
  if (!releaseGuard(value)) throw new Error('The release record format could not be confirmed; refresh to check.')
  const release = value as Release
  if (release.factory_id !== factoryId
    || (['ACTIVE', 'ACCEPTED_PENDING_EFFECTIVE'].includes(release.source_state) && !release.source_receipt_id)
    || (release.source_state === 'ACTIVE') !== (release.effective_at !== null)
    || (release.execution_state !== 'NOT_STARTED' && release.source_state !== 'ACTIVE')) {
    throw new Error('The release record does not match the factory or the execution receipt; ask the administrator to check.')
  }
  return release
}
export function parseSimulatorStatus(value: unknown, factoryId: string): SimulatorStatus {
  if (!shape({ factory_id: string, run_id: string, mode: oneOf('PAUSED', 'RUNNING'), interval_ms: (item) => number(item) && (item as number) >= 100 && (item as number) <= 60000, business_clock: timestamp, server_time: optional(timestamp),
    replay: optional(nullable(shape({ origin_run_id: string, next_revision: nonNegative, target_revision: nonNegative, done: boolean, error_code: nullable(string) }))),
    scenario: optional(nullable(shape({ enabled: boolean, seed: nonNegative, every_minutes: (item) => nonNegative(item) && Number(item) >= 30, counter: nonNegative, next_at: timestamp }))),
  })(value)
    || (value as SimulatorStatus).factory_id !== factoryId) throw new Error('The run status could not be confirmed; refresh to check.')
  return value as SimulatorStatus
}
export function parseApproval(value: unknown): Approval {
  if (!approvalGuard(value)) throw new Error('The approval result could not be confirmed; refresh the records before acting.')
  return value as Approval
}
export function parseAgentRun(value: unknown): AgentRun {
  if (!agentRunGuard(value)) throw new Error('The action result could not be confirmed; refresh to check.')
  return value as AgentRun
}
export function parseAgentRuns(value: unknown): AgentRun[] {
  if (!object(value) || !array(agentRunGuard)(value.runs)) throw new Error('The action record format is not recognized; contact the administrator.')
  return value.runs as AgentRun[]
}
