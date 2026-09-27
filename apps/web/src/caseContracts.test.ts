import { describe, expect, it } from 'vitest'
import type { CaseDetail, HumanTask } from './caseContracts'
import { parseCaseDetail, parseHumanTask } from './caseContracts'

const factoryId = 'human-review-factory', now = '2026-09-17T10:00:00Z'
function fixture(kind: 'HANDOFF' | 'APPROVAL' = 'HANDOFF') {
  const record: CaseDetail = { case_id: 'case-review', factory_id: factoryId, run_id: 'review-run', owner_id: 'planner-one', title: 'Order schedule after a machine stop', state: 'WAITING', version: 4, snapshot_id: 'snapshot-four', context: {}, closure: null, error_code: null, created_at: now, updated_at: now, inputs: [], operations: [{ operation_id: 'action-one', action: kind === 'HANDOFF' ? 'handoff' : 'request_approval', parameters: {}, snapshot_id: 'snapshot-four', state: 'DONE', result: { status: 'PENDING', summary: 'Owner task saved.' }, reason_summary: 'The recovery time is not confirmed; the owner must check the due date risk.', created_at: now }] }
  const task: HumanTask = { task_id: 'task-review', case_id: record.case_id, factory_id: factoryId, version: 2, task_type: kind, case_version: record.version, snapshot_hash: 'snapshot-hash-four', review: kind === 'APPROVAL' ? { candidate_id: 'candidate-review', required_scopes: ['publish_plan', 'allow_overtime'], outcome: 'PENDING' } : null, question: kind === 'HANDOFF' ? 'Explicitly take over the responsibility and risks of this case' : 'Review the plan and make an explicit decision', subject_id: kind === 'HANDOFF' ? record.case_id : 'candidate-review', owner_role: 'planner', owner_id: null, fields: ['comment'], state: 'OPEN', response: null, created_at: now, updated_at: now, due_at: '2026-09-17T10:30:00Z', clock: 'real', reminders_count: 0, send_state: 'NOT_ENABLED', delivery_state: 'UNAVAILABLE' }
  return { record, task }
}

describe('case and task contracts', () => {
  it('rejects oversized public reasons, information posing as a takeover, wrong roles and missing versions', () => {
    const { record, task } = fixture(); record.operations[0]!.reason_summary = 'r'.repeat(501)
    expect(() => parseCaseDetail(record, factoryId, record.case_id)).toThrow()
    expect(() => parseHumanTask({ ...task, case_version: 0 }, factoryId)).toThrow()
    expect(() => parseHumanTask({ ...task, task_type: 'UNRECOGNIZED' }, factoryId)).toThrow()
    expect(() => parseHumanTask({ ...task, response: { answer: { comment: 'confirmed' }, actor_id: 'planner-one', actor_role: 'planner', received_at: now, source: 'authenticated_human_information' } }, factoryId)).toThrow()
    expect(() => parseHumanTask({ ...task, factory_id: 'another-factory' }, factoryId)).toThrow()
  })
})
