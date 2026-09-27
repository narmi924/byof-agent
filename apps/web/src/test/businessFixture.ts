import type { BusinessOption, BusinessStudyJob, BusinessTerms } from '../businessContracts'
import { boardSnapshot, day } from './boardFixture'

export const businessTerms: BusinessTerms = {
  version: 'business-v1', evidence_mode: 'synthetic',
  delivery_rules: [{ product_id: 'BRG-6202', partial_delivery_allowed: true, minimum_partial_quantity: 50, max_deliveries: 2 }],
  expedite_quotes: [{ quote_id: 'Q1', receipt_id: 'RCPT-1', receipt_version: 1, original_eta: `${day}T08:00:00Z`, expedited_eta: `${day}T05:00:00Z`, quantity: 100, valid_until: `${day}T04:00:00Z`, source_reference: 'supplier-quote-1', evidence_mode: 'synthetic', cost_minor: 12500, currency: 'CNY' }],
}
export function businessJob(optionOverrides: Partial<BusinessOption> = {}): BusinessStudyJob {
  const snapshot = boardSnapshot()
  const request = { kind: 'urgent_order' as const, order: { order_id: 'SO-NEW', product_id: 'BRG-6202', quantity: 200, due_at: `${day}T09:00:00Z`, priority_weight: 5, hard_deadline: false, version: 1 as const, split_revision: 1 as const, status: 'CONFIRMED' as const }, partial_delivery_allowed: false, minimum_partial_quantity: null, final_due_at: null, receipt_id: null, expedite_quote_ids: [], total_time_limit: 30 }
  return {
    job_id: 'study-job-1', case_id: 'case-1', state: 'SUCCEEDED', request, error_code: null, study_hash: 'a'.repeat(64), current: true, created_at: `${day}T01:10:00Z`, advisory_only: true,
    study: { schema_version: 'byof.business-study/1', study_id: 'study-1', factory_id: snapshot.factory_id, run_id: 'run-1', origin_snapshot_hash: snapshot.content_hash, origin_snapshot_clock: snapshot.snapshot_clock, request, publishable: false, options: [{
      option_id: 'option-1', kind: 'normal', title: 'Regular-shift fulfilment', status: 'FEASIBLE', summary: 'The delivery can be met under current production conditions.', assumptions: ['Existing customer commitments are kept.'],
      allow_overtime: false, diagnostic_only: false, publishable: false, requires_business_confirmation: true, protects_existing_commitments: true,
      quote_id: null, cost_minor: null, currency: null, incremental_overtime_minutes: 0, changed_operations: 2, requested_quantity: 200, on_time_quantity: 200, completion_at: `${day}T08:00:00Z`,
      deliveries: [{ quantity: 200, ready_at: `${day}T08:00:00Z` }], earliest_completion_proven: false, maximum_on_time_quantity_proven: false,
      impacts: [{ order_id: 'SO-001', existing_commitment: true, requested_due_at: `${day}T09:00:00Z`, quantity: 100, on_time_quantity: 100, completion_at: `${day}T07:00:00Z`, tardiness_minutes: 0, baseline_completion_at: `${day}T06:00:00Z`, completion_change_minutes: 60 }], ...optionOverrides,
    }] },
  }
}
