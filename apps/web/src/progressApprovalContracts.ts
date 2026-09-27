import type { ApprovalScope } from './contracts'
import type { ValidationCertificate } from './revalidationContracts'
import { objectiveNames } from './preferenceContracts'

export type ApprovalMode = 'STRICT' | 'PROGRESS'
export interface ApprovalAttempt {
  requestId: string
  candidateHash: string
  scope: ApprovalScope
  decision: 'APPROVED' | 'REJECTED'
  mode: ApprovalMode
}

export interface ApprovalReview extends Omit<ValidationCertificate, 'certificate_id' | 'approval_ids' | 'issued_at' | 'expires_at' | 'business_expires_at'> {
  schema_version: 'byof.approval-review/1'
  review_id: string
  candidate_id: string
  approval_id: string
  approver_id: string
  approver_role: 'planner' | 'manager'
  action_scope: ApprovalScope
  reviewed_at: string
}
const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value)
export function parseApprovalReviews(value: unknown, factoryId: string): ApprovalReview[] {
  if (!Array.isArray(value)) throw new Error('The review basis could not be confirmed; refresh the records.')
  return value.map((review: unknown) => {
    if (!object(review) || review.schema_version !== 'byof.approval-review/1' || review.factory_id !== factoryId || !['review_id', 'candidate_id', 'candidate_hash', 'run_id', 'approval_id', 'approver_id', 'old_snapshot_id', 'old_snapshot_hash', 'new_snapshot_id', 'new_snapshot_hash', 'baseline_plan_version', 'baseline_plan_hash', 'remaining_plan_hash', 'source_evidence_hash', 'content_hash'].every((key) => text(review[key])) || !['publish_plan', 'allow_overtime'].includes(String(review.action_scope)) || review.approver_role !== (review.action_scope === 'publish_plan' ? 'planner' : 'manager')) throw new Error('The review record does not match the current factory, plan or approver role.')
    if (!text(review.old_source_revision) || !text(review.new_source_revision) || ![review.old_source_revision, review.new_source_revision].every((revision) => /^(0|[1-9]\d*)$/.test(revision)) || BigInt(review.new_source_revision) <= BigInt(review.old_source_revision) || review.old_snapshot_id === review.new_snapshot_id || review.old_snapshot_hash === review.new_snapshot_hash) throw new Error('The review record does not reference strictly newer factory facts.')
    const binding = review.original_binding, checker = review.checker
    if (!object(binding) || !['profile_version', 'policy_version', 'objective_version'].every((key) => text(binding[key])) || binding.snapshot_hash !== review.old_snapshot_hash || binding.baseline_plan_version !== review.baseline_plan_version || !integer(binding.planning_revision) || binding.planning_revision < 1 || !integer(binding.scope_version) || binding.scope_version < 1) throw new Error('The review record does not fully keep the version basis of the original plan.')
    if (!object(checker) || !text(checker.checker_version) || checker.status !== 'PASS' || checker.snapshot_hash !== review.new_snapshot_hash || !Array.isArray(checker.issues) || checker.issues.length !== 0) throw new Error('The full independent check on the reviewed facts did not pass.')
    if (!Array.isArray(review.metrics) || review.metrics.length !== 5 || !review.metrics.every((metric) => object(metric) && objectiveNames.some((name) => name === metric.name) && metric.unit === (metric.name === 'changed_operations' ? 'operations' : 'minutes') && (metric.lower_bound === undefined || metric.lower_bound === null) && integer(metric.value) && (metric.name === 'incremental_overtime_metric' || metric.value >= 0) && metric.unknown_reason === null) || new Set(review.metrics.map((metric: Record<string, unknown>) => metric.name)).size !== 5) throw new Error('The review metrics are incomplete or carry the optimality proof of the original solve.')
    if (!text(review.reviewed_at) || !/(?:Z|[+-]\d{2}:\d{2})$/.test(review.reviewed_at) || !Number.isFinite(Date.parse(review.reviewed_at))) throw new Error('The review decision time has no valid time zone.')
    return review as unknown as ApprovalReview
  })
}
