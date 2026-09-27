import type { Approval, ApprovalScope, CandidateRecord, Freshness, Snapshot, VersionBinding } from './contracts'
import type { EffectiveObjective } from './preferenceContracts'
import { objectiveNames } from './preferenceContracts'

export interface ValidationCertificate {
  certificate_id: string
  factory_id: string
  run_id: string
  candidate_hash: string
  approval_ids: string[]
  original_binding: VersionBinding & { planning_revision: number; scope_version: number }
  old_snapshot_id: string
  old_snapshot_hash: string
  new_snapshot_id: string
  new_snapshot_hash: string
  old_source_revision: string
  new_source_revision: string
  baseline_plan_version: string
  baseline_plan_hash: string
  remaining_plan_hash: string
  source_evidence_hash: string
  checker: { checker_version: string; snapshot_hash: string; status: 'PASS'; issues: [] }
  metrics: { name: string; value: number; unit: string; lower_bound?: null; unknown_reason: null }[]
  issued_at: string
  expires_at: string
  business_expires_at: string
  content_hash: string
}

const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value)
const timestamp = (value: unknown): value is string => text(value) && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))
export function parseValidationCertificate(value: unknown, factoryId: string): ValidationCertificate {
  if (!object(value) || !['certificate_id', 'factory_id', 'run_id', 'candidate_hash', 'old_snapshot_id', 'old_snapshot_hash', 'new_snapshot_id', 'new_snapshot_hash', 'old_source_revision', 'new_source_revision', 'remaining_plan_hash', 'source_evidence_hash', 'content_hash', 'baseline_plan_version', 'baseline_plan_hash'].every((key) => text(value[key])) || value.factory_id !== factoryId || !Array.isArray(value.approval_ids) || value.approval_ids.length === 0 || !value.approval_ids.every(text) || new Set(value.approval_ids).size !== value.approval_ids.length) throw new Error('The remaining-plan certificate does not match the factory or basis; check the original request.')
  if (!text(value.old_source_revision) || !text(value.new_source_revision) || ![value.old_source_revision, value.new_source_revision].every((revision) => /^(0|[1-9]\d*)$/.test(revision)) || BigInt(value.new_source_revision) <= BigInt(value.old_source_revision) || value.old_snapshot_id === value.new_snapshot_id || value.old_snapshot_hash === value.new_snapshot_hash) throw new Error('The check does not reference strictly newer source facts; check the records.')
  const binding = value.original_binding, checker = value.checker
  if (!object(binding) || !['snapshot_hash', 'profile_version', 'policy_version', 'objective_version'].every((key) => text(binding[key])) || !integer(binding.planning_revision) || binding.planning_revision < 1 || !integer(binding.scope_version) || binding.scope_version < 1 || !(binding.baseline_plan_version === null || text(binding.baseline_plan_version)) || binding.snapshot_hash !== value.old_snapshot_hash || binding.baseline_plan_version !== value.baseline_plan_version) throw new Error('The original plan version in the certificate is incomplete.')
  if (!object(checker) || !text(checker.checker_version) || checker.status !== 'PASS' || checker.snapshot_hash !== value.new_snapshot_hash || !Array.isArray(checker.issues) || checker.issues.length !== 0) throw new Error('The full independent check on the latest facts has not passed.')
  if (!Array.isArray(value.metrics) || value.metrics.length !== 5 || !value.metrics.every((metric) => object(metric) && objectiveNames.some((name) => name === metric.name) && (metric.unit === (metric.name === 'changed_operations' ? 'operations' : 'minutes')) && (metric.lower_bound === undefined || metric.lower_bound === null) && integer(metric.value) && (metric.name === 'incremental_overtime_metric' || metric.value >= 0) && metric.unknown_reason === null) || new Set(value.metrics.map((item: Record<string, unknown>) => item.name)).size !== 5) throw new Error('The check metrics are incomplete or carry the proof of the original solve; check the original request.')
  if (!timestamp(value.issued_at) || !timestamp(value.expires_at) || !timestamp(value.business_expires_at) || Date.parse(value.expires_at) <= Date.parse(value.issued_at) || Date.parse(value.expires_at) - Date.parse(value.issued_at) > 60_000) throw new Error('The real validity period of the certificate could not be confirmed.')
  return value as unknown as ValidationCertificate
}
export function parseValidationCertificates(value: unknown, factoryId: string): ValidationCertificate[] {
  if (!Array.isArray(value)) throw new Error('The remaining-plan check records could not be confirmed.')
  return value.map((item) => parseValidationCertificate(item, factoryId))
}

export function effectiveApprovals(record: CandidateRecord, realTime: number): Approval[] | null {
  const latest = new Map(record.approvals.map((approval) => [approval.action_scope as string, approval]))
  const required = [...new Set(['publish_plan', ...record.candidate.required_consents])]
  const approvals = required.map((scope) => latest.get(scope))
  if (approvals.some((approval) => !approval || approval.candidate_hash !== record.candidate.content_hash || approval.decision !== 'APPROVED' || Date.parse(approval.decided_at) > realTime || Date.parse(approval.expires_at) <= realTime)) return null
  return approvals as Approval[]
}
export function canWithdrawApproval(record: CandidateRecord, scope: ApprovalScope, realTime: number): boolean {
  const latest = record.approvals.filter((approval) => approval.action_scope === scope).at(-1)
  return latest !== undefined && latest.candidate_hash === record.candidate.content_hash && latest.decision === 'APPROVED' && Date.parse(latest.decided_at) <= realTime && realTime < Date.parse(latest.expires_at)
}
export function progressReviewReason(record: CandidateRecord, snapshot: Snapshot | null, objective: EffectiveObjective | null): string | null {
  if (!snapshot || snapshot.profile.policy?.progress_revalidation_enabled !== true) return 'Remaining-plan checks are not enabled for this factory.'
  if (snapshot.source.source_system === 'factory-simulator-replay') return 'A factory replay is view-only.'
  if (!snapshot.run_id || !record.run_id || snapshot.run_id !== record.run_id) return "The plan's factory run does not match or is not confirmed."
  if (!snapshot.active_plan_version || !snapshot.active_plan_hash) return 'There is no execution baseline to check normal progress against.'
  if (objective?.status !== 'READY' || objective.objective_version !== record.candidate.binding.objective_version) return 'The scheduling objective has changed or is not confirmed.'
  if (record.state !== 'STALE' || !record.candidate.has_solution || record.candidate.checker.status !== 'PASS') return 'This plan does not meet the conditions for a latest-facts check.'
  if (Date.parse(snapshot.snapshot_clock) >= Date.parse(record.candidate.accept_before)) return "The factory time is past the plan's acceptance deadline."
  return null
}
export function revalidationReason(record: CandidateRecord, snapshot: Snapshot | null, objective: EffectiveObjective | null, realTime: number): string | null {
  const reason = progressReviewReason(record, snapshot, objective)
  if (reason) return reason
  if (!effectiveApprovals(record, realTime)) return 'The original plan or overtime approval is incomplete, expired or rejected.'
  return null
}
export function certificateReason(certificate: ValidationCertificate, record: CandidateRecord, snapshot: Snapshot | null, objective: EffectiveObjective | null, freshness: Freshness, realTime: number): string | null {
  const reason = revalidationReason(record, snapshot, objective, realTime)
  if (reason) return reason
  if (!snapshot) return 'The current factory facts are missing.'
  if (certificate.candidate_hash !== record.candidate.content_hash || certificate.run_id !== record.run_id || certificate.factory_id !== snapshot.factory_id || certificate.old_snapshot_id !== record.snapshot_id || certificate.old_snapshot_hash !== record.candidate.binding.snapshot_hash || Object.entries(certificate.original_binding).some(([key, value]) => record.candidate.binding[key as keyof VersionBinding] !== value)) return 'The certificate does not match the basis of the original plan.'
  if (certificate.new_snapshot_id !== snapshot.snapshot_id || certificate.new_snapshot_hash !== snapshot.content_hash || certificate.new_source_revision !== snapshot.source.source_revision || certificate.baseline_plan_version !== snapshot.active_plan_version || certificate.baseline_plan_hash !== (snapshot.active_plan_hash ?? null)) return 'The factory facts or execution baseline changed after the check.'
  const ids = effectiveApprovals(record, realTime)!.map((approval) => approval.approval_id)
  if (ids.length !== certificate.approval_ids.length || !ids.every((id) => certificate.approval_ids.includes(id))) return 'The approvals referenced by the check have changed.'
  if (Date.parse(certificate.issued_at) > realTime || realTime >= Date.parse(certificate.expires_at)) return 'The certificate is past its real validity period or not yet valid.'
  if (Date.parse(certificate.business_expires_at) !== Date.parse(record.candidate.accept_before) || Date.parse(snapshot.snapshot_clock) >= Date.parse(certificate.business_expires_at) || Date.parse(snapshot.snapshot_clock) < Date.parse(record.candidate.effective_not_before)) return "The factory time is outside the original plan's acceptance window."
  if (freshness !== 'CURRENT' || snapshot.source.freshness !== 'CURRENT' || !snapshot.source.complete) return 'The current enterprise facts are not confirmed complete and valid; sync.'
  return null
}
