export interface StudyOrder { order_id: string; product_id: string; quantity: number; due_at: string; priority_weight: number; hard_deadline: boolean; version: 1; split_revision: 1; status: 'CONFIRMED' }
export interface BusinessStudyRequest {
  economic_priority?: 'contribution' | 'cash' | 'delivery' | 'stability' | 'overtime' | null; max_cash_outlay_minor?: number | null
  kind: 'urgent_order' | 'material_shortage' | 'production_exception'; subject_id?: string | null; order: StudyOrder | null; existing_order_id?: string | null; partial_delivery_allowed: boolean
  minimum_partial_quantity: number | null; final_due_at: string | null; receipt_id: string | null; expedite_quote_ids: string[]; total_time_limit: number
}
export interface OrderImpact {
  order_id: string; existing_commitment: boolean; requested_due_at: string; quantity: number; on_time_quantity: number | null
  completion_at: string | null; tardiness_minutes: number | null; baseline_completion_at: string | null; completion_change_minutes: number | null
}
export interface OptionEconomics {
  adverse?: { extra_cost_percent: number; completion_delay_minutes: number; net_contribution_minor: number; incremental_cash_outlay_minor: number; basis: string } | null
  catalog_version: 'byof-demo-economics/1'; evidence_mode: 'synthetic'; currency: 'SGD'; status: 'ESTIMATED' | 'INCOMPLETE'
  revenue_minor: number | null; variable_cost_minor: number | null; additional_cost_minor: number | null; late_deduction_minor: number | null
  net_contribution_minor: number | null; incremental_cash_outlay_minor: number | null; improvement_minor: number | null; comparison_option_id: string | null
  lines: { label: string; amount_minor: number; basis: string }[]; assumptions: string[]; missing: string[]
}
export interface BusinessOption {
  option_id: string; kind: 'normal' | 'overtime' | 'earliest_completion' | 'partial_delivery' | 'shared_material' | 'receipt_expedite' | 'wait_diagnostic' | 'treatment'
  title: string; status: 'FEASIBLE' | 'INFEASIBLE' | 'UNKNOWN' | 'BLOCKED' | 'BUDGET_EXHAUSTED' | 'CHECK_FAILED'; summary: string; assumptions: string[]
  allow_overtime: boolean; diagnostic_only: boolean; publishable: false; requires_business_confirmation: true; protects_existing_commitments: boolean | null
  quote_id: string | null; cost_minor: number | null; currency: string | null; incremental_overtime_minutes: number | null; changed_operations: number | null
  requested_quantity: number | null; on_time_quantity: number | null; completion_at: string | null
  deliveries: { quantity: number; ready_at: string }[]; earliest_completion_proven: boolean; maximum_on_time_quantity_proven: boolean; impacts: OrderImpact[]
  economics?: OptionEconomics | null
  dominated?: boolean
  actions?: { kind: 'supply' | 'repair' | 'staff' | 'order_due' | 'order_quantity'; target_id: string; action_id: string; ready_at: string; quantity: number; expected_version: number; mode: 'standard' | 'express' | 'immediate' }[]
}
export interface BusinessStudy {
  schema_version: 'byof.business-study/1'; study_id: string; factory_id: string; run_id: string; origin_snapshot_hash: string; origin_snapshot_clock: string
  request: BusinessStudyRequest; options: BusinessOption[]; publishable: false
}
export interface BusinessStudyJob {
  job_id: string; case_id: string | null; state: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'; request: BusinessStudyRequest
  error_code: string | null; study: BusinessStudy | null; study_hash: string | null; current: boolean; created_at: string; advisory_only: true
}
export interface ExpediteQuote { quote_id: string; receipt_id: string; receipt_version: number; original_eta: string; expedited_eta: string; quantity: number; valid_until: string; source_reference: string; evidence_mode: 'synthetic' | 'enterprise'; cost_minor: number | null; currency: string | null }
export interface BusinessTerms { version: string; evidence_mode: 'synthetic' | 'enterprise'; delivery_rules: { product_id: string; partial_delivery_allowed: boolean; minimum_partial_quantity: number; max_deliveries: number }[]; expedite_quotes: ExpediteQuote[] }

const object = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const text = (v: unknown): v is string => typeof v === 'string' && v.length > 0
const integer = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v)
const nonNegative = (v: unknown): v is number => integer(v) && v >= 0
const positive = (v: unknown): v is number => integer(v) && v > 0
const timestamp = (v: unknown): v is string => typeof v === 'string' && /T.*(?:Z|[+-]\d\d:\d\d)$/.test(v) && Number.isFinite(Date.parse(v))
const nullable = (v: unknown, guard: (value: unknown) => boolean) => v === null || guard(v)
const list = (v: unknown, guard: (value: unknown) => boolean) => Array.isArray(v) && v.every(guard)
const currency = (v: unknown): v is string => typeof v === 'string' && /^[A-Z]{3}$/.test(v)

export function isBusinessStudyRequest(v: unknown): v is BusinessStudyRequest {
  if (!object(v) || !['urgent_order', 'material_shortage', 'production_exception'].includes(String(v.kind)) || typeof v.partial_delivery_allowed !== 'boolean'
    || !nullable(v.minimum_partial_quantity, positive) || !nullable(v.final_due_at, timestamp) || !nullable(v.receipt_id, text)
    || !Array.isArray(v.expedite_quote_ids) || !v.expedite_quote_ids.every(text) || v.expedite_quote_ids.length > 4
    || new Set(v.expedite_quote_ids).size !== v.expedite_quote_ids.length || !positive(v.total_time_limit) || v.total_time_limit > (v.kind === 'production_exception' ? 120 : 60)) return false
  if (v.existing_order_id !== undefined && !nullable(v.existing_order_id, text)) return false
  if (v.economic_priority !== undefined && v.economic_priority !== null && !['contribution', 'cash', 'delivery', 'stability', 'overtime'].includes(String(v.economic_priority))) return false
  if (v.max_cash_outlay_minor !== undefined && !nullable(v.max_cash_outlay_minor, nonNegative)) return false
  if (v.kind === 'production_exception') return v.final_due_at === null && v.minimum_partial_quantity === null && v.order === null && !v.partial_delivery_allowed && v.expedite_quote_ids.length === 0 && v.receipt_id === null && (v.subject_id === undefined || nullable(v.subject_id, text))
  if (v.kind === 'material_shortage') return v.order === null && (v.existing_order_id === undefined || v.existing_order_id === null) && !v.partial_delivery_allowed && v.minimum_partial_quantity === null && v.final_due_at === null
  if (v.receipt_id !== null || v.expedite_quote_ids.length !== 0 || (!v.partial_delivery_allowed && v.minimum_partial_quantity !== null)) return false
  if (text(v.existing_order_id)) return v.order === null
  const o = v.order
  return object(o) && text(o.order_id) && text(o.product_id) && positive(o.quantity) && timestamp(o.due_at) && positive(o.priority_weight)
    && typeof o.hard_deadline === 'boolean' && o.version === 1 && o.split_revision === 1 && o.status === 'CONFIRMED'
    && v.receipt_id === null && v.expedite_quote_ids.length === 0 && (v.partial_delivery_allowed || v.minimum_partial_quantity === null)
    && (v.final_due_at === null || Date.parse(v.final_due_at as string) >= Date.parse(o.due_at))
}

function impact(v: unknown): v is OrderImpact {
  return object(v) && text(v.order_id) && typeof v.existing_commitment === 'boolean' && timestamp(v.requested_due_at) && nonNegative(v.quantity)
    && nullable(v.on_time_quantity, nonNegative) && nullable(v.completion_at, timestamp) && nullable(v.tardiness_minutes, nonNegative)
    && nullable(v.baseline_completion_at, timestamp) && nullable(v.completion_change_minutes, integer)
}
function option(v: unknown): v is BusinessOption {
  return object(v) && ['option_id', 'title', 'summary'].every(k => text(v[k]))
    && ['normal', 'overtime', 'earliest_completion', 'partial_delivery', 'shared_material', 'receipt_expedite', 'wait_diagnostic', 'treatment'].includes(String(v.kind))
    && ['FEASIBLE', 'INFEASIBLE', 'UNKNOWN', 'BLOCKED', 'BUDGET_EXHAUSTED', 'CHECK_FAILED'].includes(String(v.status))
    && list(v.assumptions, text) && typeof v.allow_overtime === 'boolean' && typeof v.diagnostic_only === 'boolean' && v.publishable === false && v.requires_business_confirmation === true
    && (v.protects_existing_commitments === null || typeof v.protects_existing_commitments === 'boolean')
    && nullable(v.quote_id, text) && nullable(v.cost_minor, nonNegative) && nullable(v.currency, currency) && (v.cost_minor === null) === (v.currency === null)
    && nullable(v.incremental_overtime_minutes, integer) && nullable(v.changed_operations, nonNegative) && nullable(v.requested_quantity, nonNegative)
    && nullable(v.on_time_quantity, nonNegative) && nullable(v.completion_at, timestamp)
    && list(v.deliveries, x => object(x) && positive(x.quantity) && timestamp(x.ready_at)) && list(v.impacts, impact)
    && typeof v.earliest_completion_proven === 'boolean' && typeof v.maximum_on_time_quantity_proven === 'boolean'
    && (v.actions === undefined || list(v.actions, a => object(a) && ['supply', 'repair', 'staff', 'order_due', 'order_quantity'].includes(String(a.kind)) && text(a.target_id) && text(a.action_id) && timestamp(a.ready_at) && nonNegative(a.quantity) && positive(a.expected_version) && ['standard', 'express', 'immediate'].includes(String(a.mode))))
    && (v.economics === undefined || v.economics === null || economics(v.economics))
    && (v.dominated === undefined || typeof v.dominated === 'boolean')
}
function economics(v: unknown): v is OptionEconomics {
  return object(v) && v.catalog_version === 'byof-demo-economics/1' && v.evidence_mode === 'synthetic' && v.currency === 'SGD'
    && ['ESTIMATED', 'INCOMPLETE'].includes(String(v.status))
    && ['revenue_minor', 'variable_cost_minor', 'additional_cost_minor', 'late_deduction_minor', 'incremental_cash_outlay_minor'].every(k => nullable(v[k], nonNegative))
    && nullable(v.net_contribution_minor, integer) && nullable(v.improvement_minor, integer) && nullable(v.comparison_option_id, text)
    && list(v.lines, line => object(line) && text(line.label) && nonNegative(line.amount_minor) && text(line.basis))
    && list(v.assumptions, text) && list(v.missing, text)
    && (v.adverse === undefined || v.adverse === null || (object(v.adverse) && nonNegative(v.adverse.extra_cost_percent) && nonNegative(v.adverse.completion_delay_minutes) && integer(v.adverse.net_contribution_minor) && nonNegative(v.adverse.incremental_cash_outlay_minor) && text(v.adverse.basis)))
}
function study(v: unknown): v is BusinessStudy {
  return object(v) && v.schema_version === 'byof.business-study/1' && ['study_id', 'factory_id', 'run_id', 'origin_snapshot_hash'].every(k => text(v[k]))
    && timestamp(v.origin_snapshot_clock) && isBusinessStudyRequest(v.request) && list(v.options, option) && v.publishable === false
}
export function parseBusinessStudyJob(v: unknown): BusinessStudyJob {
  if (!object(v) || !text(v.job_id) || !['QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'].includes(String(v.state))
    || !isBusinessStudyRequest(v.request) || !nullable(v.error_code, text) || !nullable(v.study, study) || !nullable(v.study_hash, text)
    || (v.case_id !== undefined && !nullable(v.case_id, text)) || typeof v.current !== 'boolean' || !timestamp(v.created_at) || v.advisory_only !== true) throw new Error('The option comparison result is not recognized; reload it.')
  return { ...v, case_id: v.case_id ?? null } as unknown as BusinessStudyJob
}
export function isBusinessTerms(v: unknown): v is BusinessTerms {
  return object(v) && text(v.version) && ['synthetic', 'enterprise'].includes(String(v.evidence_mode))
    && list(v.delivery_rules, r => object(r) && text(r.product_id) && typeof r.partial_delivery_allowed === 'boolean' && positive(r.minimum_partial_quantity) && positive(r.max_deliveries))
    && list(v.expedite_quotes, q => object(q) && ['quote_id', 'receipt_id', 'source_reference'].every(k => text(q[k])) && positive(q.receipt_version) && positive(q.quantity)
      && ['original_eta', 'expedited_eta', 'valid_until'].every(k => timestamp(q[k])) && ['synthetic', 'enterprise'].includes(String(q.evidence_mode))
      && nullable(q.cost_minor, nonNegative) && nullable(q.currency, currency) && (q.cost_minor === null) === (q.currency === null))
}
export function quotedCost(minor: number | null, code: string | null): string {
  if (minor === null || code === null) return 'Cost not confirmed'
  const format = new Intl.NumberFormat('en-US', { style: 'currency', currency: code, currencyDisplay: 'code' })
  return format.format(minor / 10 ** (format.resolvedOptions().maximumFractionDigits ?? 2))
}

/** The options a manager can act on: feasible, costed, not dominated and not repeated. */
export function actionableOptions(job: BusinessStudyJob): BusinessOption[] {
  const options = job.study?.options ?? []
  if (job.request.kind !== 'production_exception') return options
  const signatures = new Set<string>()
  return options.filter(option => {
    if (option.status !== 'FEASIBLE' || option.economics?.status !== 'ESTIMATED' || option.diagnostic_only || option.dominated) return false
    const signature = JSON.stringify([option.economics.net_contribution_minor, option.economics.incremental_cash_outlay_minor, option.completion_at, option.incremental_overtime_minutes, option.actions?.map(action => [action.kind, action.target_id, action.quantity, action.ready_at])])
    if (signatures.has(signature)) return false
    signatures.add(signature); return true
  }).slice(0, 3)
}
