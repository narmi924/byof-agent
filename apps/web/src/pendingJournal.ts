import { useMemo, useState, useSyncExternalStore } from 'react'
import { isBusinessStudyRequest } from './businessContracts'
import type { ApprovalScope, ReplayRequest, SimulatorCommand, TodayRunRequest } from './contracts'
import type { ApprovalMode } from './progressApprovalContracts'
import type { TaskAction } from './HumanTasks'
import { taskRoles } from './caseContracts'
import type { ContactInput } from './notificationContracts'
import type { ObjectiveDefinition, PreferenceHead, PreferenceProposal, ProposalInput } from './preferenceContracts'
import { parseObjectiveDefinition, parsePreferenceProposal } from './preferenceContracts'

export type PlanningAttempt =
  | { kind: 'solve'; requestId: string; allowOvertime: boolean; earliest: string | null }
  | { kind: 'agent'; requestId: string; message: string }
  | { kind: 'approval'; requestId: string; candidateId: string; candidateHash: string; scope: ApprovalScope; decision: 'APPROVED' | 'REJECTED'; mode: ApprovalMode }
  | { kind: 'validation'; requestId: string; candidateId: string; candidateHash: string }
  | { kind: 'publication'; requestId: string; candidateId: string; candidateHash: string; certificateId?: string }
export interface CaseAttempt { kind: 'create' | 'message' | TaskAction; caseId: string | null; taskId: string | null; body: Record<string, unknown>; success: string }
export type ManagementAttempt = { command: SimulatorCommand; label: string } | { replay: ReplayRequest; label: string } | { today: TodayRunRequest; label: string }
export type PreferenceAttempt = { kind: 'propose'; body: ProposalInput } | { kind: 'confirm'; proposal: PreferenceProposal; body: { request_id: string; expected_state_version: number } } | { kind: 'reject'; proposalId: string; body: { request_id: string; reason: string } } | { kind: 'deactivate'; head: PreferenceHead; body: { request_id: string; expected_state_version: number; expected_version: number; reason: string } } | { kind: 'coordinate'; body: { request_id: string; expected_state_version: number; context_hash: string; definition: ObjectiveDefinition; reason: string } }
export function preferenceAttempt(value: PreferenceAttempt): PreferenceAttempt {
  if (value.kind === 'confirm') {
    const { proposal_id, state, scope_type, scope_id, definition, expected_version, created_at, proposer_id, reason } = value.proposal
    return { kind: value.kind, body: value.body, proposal: { proposal_id, state, scope_type, scope_id, definition, expected_version, created_at, proposer_id, reason } }
  }
  if (value.kind === 'deactivate') {
    const { scope_type, scope_id, version, active, preference_id, definition } = value.head
    return { kind: value.kind, body: value.body, head: { scope_type, scope_id, version, active, preference_id, definition } }
  }
  return value
}
interface SlotTypes { planning: PlanningAttempt; cases: CaseAttempt; simulator: ManagementAttempt; contacts: ContactInput; preferences: PreferenceAttempt; assistant: import('./assistantContracts').AssistantAttempt }
type Category = keyof SlotTypes
const changed = 'byof-pending-changed'
const prefix = 'byof.pending.v1:'
const maxLength = 65_536
const invalid = 'The pending action record is damaged or unsupported. Keep this tab open and ask the administrator to check the original request; new actions are blocked.'
const unavailable = 'Cannot save the pending action. Allow session storage for this tab and try again; the request has not been sent.'
const retiredBusiness = 'The old option comparison or acceptance entry has been retired, and the original request will not be resent. Keep the old record first, then continue; plans are now discussed with the manager in the conversation and measures run after explicit manager approval.'
const recordError = (reason: unknown) => reason instanceof Error && reason.message === retiredBusiness ? retiredBusiness : invalid

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error(invalid)
  return value as Record<string, unknown>
}
function keys(value: unknown, required: string[], optional: string[] = []): Record<string, unknown> {
  const result = object(value)
  if (required.some((key) => !Object.hasOwn(result, key)) || Object.keys(result).some((key) => !required.includes(key) && !optional.includes(key))) throw new Error(invalid)
  return result
}
function assert(value: unknown): asserts value { if (!value) throw new Error(invalid) }
const text = (value: unknown, max = 8000): value is string => typeof value === 'string' && value.trim().length > 0 && value.length <= max
const id = (value: unknown): value is string => typeof value === 'string' && /^[^\s]{1,160}$/.test(value) && [...value].every((character) => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127)
const digest = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const integer = (value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum && value <= maximum
const date = (value: unknown): value is string => typeof value === 'string' && value.length <= 40 && /T.*(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value))
const minute = (value: unknown): value is string => date(value) && Date.parse(value) % 60_000 === 0
const oneOf = (value: unknown, values: readonly string[]) => typeof value === 'string' && values.includes(value)
function definition(value: unknown) {
  keys(value, ['selection', 'objective_order', 'max_weighted_tardiness', 'max_incremental_overtime_minutes'])
  parseObjectiveDefinition(value)
}
function scope(value: Record<string, unknown>, factoryId: string) {
  assert(oneOf(value.scope_type, ['FACTORY', 'PROCESS', 'CASE']) && id(value.scope_id))
  assert(value.scope_type !== 'FACTORY' || value.scope_id === factoryId)
}
function validatePlanning(value: unknown) {
  const item = object(value)
  assert(id(item.requestId))
  if (item.kind === 'solve') { keys(item, ['kind', 'requestId', 'allowOvertime', 'earliest']); assert(typeof item.allowOvertime === 'boolean' && (item.earliest === null || date(item.earliest))); return }
  if (item.kind === 'agent') { keys(item, ['kind', 'requestId', 'message']); assert(text(item.message)); return }
  assert(id(item.candidateId) && id(item.candidateHash))
  if (item.kind === 'approval') {
    keys(item, ['kind', 'requestId', 'candidateId', 'candidateHash', 'scope', 'decision', 'mode'])
    assert(oneOf(item.scope, ['publish_plan', 'allow_overtime']) && oneOf(item.decision, ['APPROVED', 'REJECTED']) && oneOf(item.mode, ['STRICT', 'PROGRESS']))
    assert(item.mode !== 'PROGRESS' || item.decision === 'APPROVED'); return
  }
  assert(item.kind === 'validation' || item.kind === 'publication')
  keys(item, ['kind', 'requestId', 'candidateId', 'candidateHash'], item.kind === 'publication' ? ['certificateId'] : [])
  assert(item.certificateId === undefined || id(item.certificateId))
}
function validateCase(value: unknown) {
  const item = keys(value, ['kind', 'caseId', 'taskId', 'body', 'success']), body = object(item.body)
  assert(id(body.request_id) && text(item.success, 200))
  if (item.kind === 'create' || item.kind === 'message') {
    keys(body, ['request_id', 'message', ...(item.kind === 'create' ? ['start_new'] : [])], ['suggestion_id'])
    assert(text(body.message) && (body.suggestion_id === undefined || digest(body.suggestion_id)) && item.taskId === null && (item.kind === 'create' ? item.caseId === null && body.start_new === true : id(item.caseId))); return
  }
  assert(id(item.caseId) && id(item.taskId) && integer(body.expected_task_version, 1))
  if (item.kind === 'responses') {
    keys(body, ['request_id', 'expected_task_version', 'answer'])
    const answer = keys(body.answer, [], ['repair_eta', 'receipt_eta', 'remaining_minutes', 'remaining_setup_minutes', 'comment'])
    assert(Object.keys(answer).length > 0)
    for (const [key, field] of Object.entries(answer)) assert(key.endsWith('_eta') ? date(field) : key.endsWith('_minutes') ? integer(field) : text(field, 4000))
  } else if (item.kind === 'transfers') {
    keys(body, ['request_id', 'expected_task_version', 'target_role', 'target_owner_id', 'reason'])
    assert(oneOf(body.target_role, taskRoles) && body.target_owner_id === null && text(body.reason, 500))
  } else if (item.kind === 'cancellations') { keys(body, ['request_id', 'expected_task_version', 'reason']); assert(text(body.reason, 500)) }
  else {
    assert(item.kind === 'handoffs')
    keys(body, ['request_id', 'expected_task_version', 'expected_case_version', 'expected_snapshot_hash', 'accept_responsibility', 'accept_risks', 'responsibility_summary', 'risk_summary'])
    assert(integer(body.expected_case_version, 1) && id(body.expected_snapshot_hash) && body.accept_responsibility === true && body.accept_risks === true && text(body.responsibility_summary, 2000) && text(body.risk_summary, 2000))
  }
}
function validateManagement(value: unknown) {
  const item = object(value); assert(text(item.label, 100))
  if ('replay' in item) { keys(item, ['replay', 'label']); const body = keys(item.replay, ['request_id', 'expected_run_id']); assert(id(body.request_id) && id(body.expected_run_id)); return }
  if ('today' in item) { keys(item, ['today', 'label']); const body = keys(item.today, ['request_id', 'expected_run_id'], ['scenario_version']); assert(id(body.request_id) && id(body.expected_run_id) && (body.scenario_version === undefined || body.scenario_version === 'workshop-full-2')); return }
  keys(item, ['command', 'label'])
  const body = keys(item.command, ['request_id', 'run_id', 'kind', 'payload']), payload = object(body.payload)
  assert(id(body.request_id) && id(body.run_id))
  switch (body.kind) {
    case 'clock.step': keys(payload, ['minutes']); assert(integer(payload.minutes, 1, 60)); break
    case 'clock.run': keys(payload, ['interval_ms']); assert(integer(payload.interval_ms, 100, 60000)); break
    case 'clock.pause': keys(payload, []); break
    case 'resource.down': case 'resource.restore': keys(payload, ['resource_id']); assert(id(payload.resource_id)); break
    case 'resource.outage': keys(payload, ['resource_id', 'minutes']); assert(id(payload.resource_id) && integer(payload.minutes, 1, 240)); break
    case 'worker.absent': case 'worker.return': keys(payload, ['worker_id']); assert(id(payload.worker_id)); break
    case 'worker.leave': keys(payload, ['worker_id', 'minutes']); assert(id(payload.worker_id) && integer(payload.minutes, 1, 240)); break
    case 'scenario.configure': keys(payload, ['enabled', 'seed', 'every_minutes']); assert(typeof payload.enabled === 'boolean' && integer(payload.seed, 0, 2_147_483_647) && integer(payload.every_minutes, 30, 480)); break
    case 'receipt.receive': case 'receipt.cancel': keys(payload, ['receipt_id']); assert(id(payload.receipt_id)); break
    case 'receipt.delay': keys(payload, ['receipt_id', 'eta']); assert(id(payload.receipt_id) && date(payload.eta)); break
    case 'receipt.shortfall': keys(payload, ['receipt_id', 'quantity']); assert(id(payload.receipt_id) && integer(payload.quantity, 1)); break
    case 'receipt.add': keys(payload, ['receipt_id', 'material_id', 'quantity', 'eta']); assert(id(payload.receipt_id) && id(payload.material_id) && integer(payload.quantity, 1) && date(payload.eta)); break
    case 'inventory.reconcile': keys(payload, ['material_id', 'expected_version', 'counted_on_hand', 'reason']); assert(id(payload.material_id) && integer(payload.expected_version, 1) && integer(payload.counted_on_hand) && oneOf(payload.reason, ['COUNT_CORRECTION', 'SCRAP'])); break
    case 'execution.confirm_remaining': keys(payload, ['operation_id', 'remaining_minutes', 'remaining_setup_minutes']); assert(id(payload.operation_id) && integer(payload.remaining_minutes, 1) && integer(payload.remaining_setup_minutes)); break
    case 'quality.record': keys(payload, ['operation_id', 'quality_state'], ['evidence']); assert(id(payload.operation_id) && oneOf(payload.quality_state, ['PASSED', 'FAILED', 'UNKNOWN']) && (payload.evidence === undefined || text(payload.evidence, 500))); break
    case 'quality.scrap': keys(payload, ['operation_id', 'reason']); assert(id(payload.operation_id) && text(payload.reason, 500)); break
    case 'order.add':
      keys(payload, ['order_id', 'product_id', 'quantity', 'due_at', 'priority_weight', 'hard_deadline', 'version', 'split_revision', 'status'])
      assert(id(payload.order_id) && id(payload.product_id) && integer(payload.quantity, 1) && date(payload.due_at) && integer(payload.priority_weight, 1) && typeof payload.hard_deadline === 'boolean' && payload.version === 1 && payload.split_revision === 1 && payload.status === 'CONFIRMED'); break
    case 'order.revise':
      keys(payload, ['order_id', 'expected_version', 'quantity', 'due_at', 'priority_weight', 'hard_deadline'])
      assert(id(payload.order_id) && integer(payload.expected_version, 1) && integer(payload.quantity, 0) && date(payload.due_at) && integer(payload.priority_weight, 1) && typeof payload.hard_deadline === 'boolean'); break
    case 'delivery_rule.set':
      keys(payload, ['expected_terms_version', 'product_id', 'partial_delivery_allowed', 'minimum_partial_quantity', 'max_deliveries'])
      assert((payload.expected_terms_version === null || id(payload.expected_terms_version)) && id(payload.product_id) && typeof payload.partial_delivery_allowed === 'boolean' && integer(payload.minimum_partial_quantity, 1) && integer(payload.max_deliveries, 1, 2) && (!payload.partial_delivery_allowed || payload.max_deliveries === 2)); break
    case 'expedite_quote.set':
      keys(payload, ['expected_terms_version', 'quote_id', 'receipt_id', 'expected_receipt_version', 'expedited_eta', 'valid_until', 'source_reference', 'cost_minor', 'currency'])
      assert((payload.expected_terms_version === null || id(payload.expected_terms_version)) && id(payload.quote_id) && id(payload.receipt_id) && integer(payload.expected_receipt_version, 1) && minute(payload.expedited_eta) && minute(payload.valid_until) && id(payload.source_reference))
      assert((payload.cost_minor === null && payload.currency === null) || (integer(payload.cost_minor) && oneOf(payload.currency, ['CNY', 'USD', 'EUR']))); break
    case 'expedite_quote.remove':
      keys(payload, ['expected_terms_version', 'quote_id'])
      assert((payload.expected_terms_version === null || id(payload.expected_terms_version)) && id(payload.quote_id)); break
    case 'overtime_window.set':
      keys(payload, ['target_type', 'target_id', 'expected_version', 'action', 'start_at', 'end_at'])
      assert(oneOf(payload.target_type, ['worker', 'resource']) && id(payload.target_id) && integer(payload.expected_version, 1) && oneOf(payload.action, ['add', 'remove']) && minute(payload.start_at) && minute(payload.end_at) && Date.parse(payload.end_at) > Date.parse(payload.start_at)); break
    default: throw new Error(invalid)
  }
}
function validatePreference(value: unknown, factoryId: string) {
  const item = object(value), body = object(item.body); assert(id(body.request_id))
  if (item.kind === 'propose') {
    keys(item, ['kind', 'body']); keys(body, ['request_id', 'scope_type', 'scope_id', 'definition', 'expected_version', 'reason'], ['source_proposal_id'])
    scope(body, factoryId); definition(body.definition); assert(integer(body.expected_version) && text(body.reason, 500) && (body.source_proposal_id === undefined || id(body.source_proposal_id)))
  } else if (item.kind === 'confirm') {
    keys(item, ['kind', 'body', 'proposal']); keys(body, ['request_id', 'expected_state_version']); assert(integer(body.expected_state_version))
    const proposal = keys(item.proposal, ['proposal_id', 'state', 'scope_type', 'scope_id', 'definition', 'expected_version', 'created_at', 'proposer_id', 'reason'])
    scope(proposal, factoryId); definition(proposal.definition); parsePreferenceProposal(proposal)
  } else if (item.kind === 'reject') { keys(item, ['kind', 'body', 'proposalId']); keys(body, ['request_id', 'reason']); assert(id(item.proposalId) && text(body.reason, 500)) }
  else if (item.kind === 'deactivate') {
    keys(item, ['kind', 'body', 'head']); keys(body, ['request_id', 'expected_state_version', 'expected_version', 'reason'])
    const head = keys(item.head, ['scope_type', 'scope_id', 'version', 'active', 'preference_id', 'definition'])
    scope(head, factoryId); definition(head.definition)
    assert(integer(body.expected_state_version) && integer(body.expected_version, 1) && text(body.reason, 500) && integer(head.version, 1) && head.version === body.expected_version && typeof head.active === 'boolean' && id(head.preference_id))
  } else {
    assert(item.kind === 'coordinate'); keys(item, ['kind', 'body']); keys(body, ['request_id', 'expected_state_version', 'context_hash', 'definition', 'reason'])
    assert(integer(body.expected_state_version) && id(body.context_hash) && text(body.reason, 500)); definition(body.definition)
  }
}
function retiredSlot(slot: string, value: unknown): boolean {
  let payload: Record<string, unknown>
  if (slot === 'business') {
    const body = keys(value, ['request_id', 'run_id', 'payload'])
    assert(id(body.request_id) && id(body.run_id))
    payload = object(body.payload)
  } else if (slot === 'assistant') {
    const item = object(value), body = object(item.body)
    if (item.kind !== 'study' && !(item.kind === 'action' && body.kind === 'business_accept')) return false
    keys(item, ['kind', 'body', 'caseId', 'taskId'])
    assert(id(body.request_id) && id(body.run_id) && (item.caseId === null || id(item.caseId)) && (item.taskId === null || id(item.taskId)))
    if (item.kind === 'study') {
      keys(body, ['request_id', 'run_id', 'expected_snapshot_hash', 'request'])
      assert(id(body.expected_snapshot_hash) && isBusinessStudyRequest(body.request))
      return true
    }
    keys(body, ['request_id', 'run_id', 'kind', 'payload'])
    payload = object(body.payload)
  } else return false
  keys(payload, ['job_id', 'option_id', 'study_hash', 'confirm_extra_cost', 'allow_overtime'])
  assert(id(payload.job_id) && id(payload.option_id) && digest(payload.study_hash) && typeof payload.confirm_extra_cost === 'boolean' && typeof payload.allow_overtime === 'boolean')
  return true
}
function validateSlot(slot: string, value: unknown, factoryId: string) {
  if (retiredSlot(slot, value)) throw new Error(retiredBusiness)
  if (slot === 'assistant') {
    const item = keys(value, ['kind', 'body', 'caseId', 'taskId']), body = object(item.body)
    assert(oneOf(item.kind, ['action', 'chat', 'response', 'stop', 'execution']) && id(body.request_id))
    assert(item.caseId === null || id(item.caseId))
    assert(item.taskId === null || id(item.taskId))
    if (item.kind === 'action') {
      keys(body, ['request_id', 'run_id', 'kind', 'payload'])
      assert(id(body.run_id) && oneOf(body.kind, ['start', 'approve', 'recover', 'outage', 'scenario', 'preference', 'reset', 'treatment_execute']))
      const payload = object(body.payload)
      if (body.kind === 'recover') {
        keys(payload, ['case_id', 'path_id'])
        assert(id(payload.case_id) && id(payload.path_id))
      } else if (body.kind === 'treatment_execute') {
        keys(payload, ['job_id', 'option_id', 'study_hash', 'allow_overtime', 'accept_customer_change'])
        assert(id(payload.job_id) && id(payload.option_id) && digest(payload.study_hash) && typeof payload.allow_overtime === 'boolean' && typeof payload.accept_customer_change === 'boolean')
      }
    } else if (item.kind === 'execution') {
      keys(body, ['request_id', 'decision'])
      assert(id(item.taskId) && oneOf(body.decision, ['cancel', 'resume']))
    } else if (item.kind === 'chat') {
      keys(body, ['request_id', 'message'], ['start_new', 'suggestion_id'])
      assert(text(body.message) && (body.start_new === undefined || typeof body.start_new === 'boolean') && (body.suggestion_id === undefined || digest(body.suggestion_id)))
    } else if (item.kind === 'stop') {
      keys(body, ['request_id', 'expected_target'])
      assert(id(item.caseId) && item.taskId === null && id(body.expected_target))
    } else {
      keys(body, ['request_id', 'expected_task_version', 'answer'])
      assert(id(item.taskId) && integer(body.expected_task_version, 1))
      object(body.answer)
    }
  }
  else if (slot === 'planning') validatePlanning(value)
  else if (slot === 'cases') validateCase(value)
  else if (slot === 'simulator') validateManagement(value)
  else if (slot === 'contacts') {
    const body = keys(value, ['request_id', 'role', 'user_id', 'email', 'enabled', 'expected_version'])
    assert(id(body.request_id) && oneOf(body.role, taskRoles) && id(body.user_id) && text(body.email, 254) && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(body.email) && typeof body.enabled === 'boolean' && integer(body.expected_version))
  } else {
    assert(slot === 'preferences' || (slot.startsWith('preferences:') && id(slot.slice(12))))
    validatePreference(value, factoryId)
  }
}
export function journalKey(userId: string, factoryId: string): string { assert(id(userId) && id(factoryId)); return `${prefix}${encodeURIComponent(userId)}:${encodeURIComponent(factoryId)}` }
function decode(raw: string | null, userId: string, factoryId: string, allowRetired = false): Record<string, unknown> {
  if (raw === null) return {}
  assert(raw.length <= maxLength)
  const root = keys(JSON.parse(raw), ['version', 'userId', 'factoryId', 'slots'])
  assert(root.version === 1 && root.userId === userId && root.factoryId === factoryId)
  const slots = object(root.slots); assert(Object.keys(slots).length <= 24)
  let retired = false
  for (const [slot, value] of Object.entries(slots)) {
    if (retiredSlot(slot, value)) retired = true
    else validateSlot(slot, value, factoryId)
  }
  if (retired && !allowRetired) throw new Error(retiredBusiness)
  return slots
}
function archiveRetired(userId: string | undefined, factoryId: string, expectedRaw: string | null) {
  if (!userId) throw new Error('The current account ID is not confirmed; sign in again before acting.')
  const key = journalKey(userId, factoryId)
  try {
    const raw = sessionStorage.getItem(key)
    assert(raw !== null && raw === expectedRaw)
    const slots = decode(raw, userId, factoryId, true)
    const obsolete = Object.keys(slots).filter(slot => retiredSlot(slot, slots[slot]))
    assert(obsolete.length > 0)
    const archiveKey = `${key}:retired-business:${crypto.randomUUID()}`
    assert(sessionStorage.getItem(archiveKey) === null)
    sessionStorage.setItem(archiveKey, raw)
    assert(sessionStorage.getItem(archiveKey) === raw)
    assert(sessionStorage.getItem(key) === raw)
    for (const slot of obsolete) delete slots[slot]
    const next = JSON.stringify({ version: 1, userId, factoryId, slots })
    sessionStorage.setItem(key, next)
    assert(sessionStorage.getItem(key) === next)
  } catch {
    throw new Error('Recovering the old record did not finish, so submissions are blocked. Allow session storage and try again; the original request will not be resent.')
  }
  window.dispatchEvent(new Event(changed))
}
function serial(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(serial).join(',')}]`
  if (typeof value === 'object' && value !== null) return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${serial(item)}`).join(',')}}`
  return JSON.stringify(value)
}
export function writePending(userId: string | undefined, factoryId: string, slot: string, value: unknown, remove = false) {
  if (!userId) throw new Error('The current account ID is not confirmed; sign in again before acting.')
  const key = journalKey(userId, factoryId)
  let raw: string | null
  try { raw = sessionStorage.getItem(key) } catch { throw new Error(unavailable) }
  let slots: Record<string, unknown>
  try { slots = decode(raw, userId, factoryId); validateSlot(slot, value, factoryId) } catch (reason) { throw new Error(recordError(reason), { cause: reason }) }
  if (slots[slot] && serial(slots[slot]) !== serial(value)) throw new Error('The previous action result is pending; check the original request first. Another action cannot be submitted.')
  if (remove) delete slots[slot]
  else slots[slot] = value
  const next = JSON.stringify({ version: 1, userId, factoryId, slots })
  if (next.length > maxLength || Object.keys(slots).length > 24) throw new Error('The pending action record is full; check the original request first.')
  try {
    sessionStorage.setItem(key, next)
    if (sessionStorage.getItem(key) !== next) throw new Error(unavailable)
  } catch { throw new Error(remove ? 'The original request was checked, but the local record was not cleared. Check the original request again; do not create a new action.' : unavailable) }
  window.dispatchEvent(new Event(changed))
}
function subscribe(listener: () => void) { window.addEventListener(changed, listener); window.addEventListener('storage', listener); return () => { window.removeEventListener(changed, listener); window.removeEventListener('storage', listener) } }
export function usePendingSlot<C extends Category>(category: C, userId: string | undefined, factoryId: string, caseId?: string) {
  const slot = category === 'preferences' && caseId ? `preferences:${caseId}` : category
  const [writeError, setWriteError] = useState('')
  const raw = useSyncExternalStore(subscribe, () => {
    if (!userId) return null
    try { return sessionStorage.getItem(journalKey(userId, factoryId)) } catch { return '!unavailable' }
  })
  const state = useMemo(() => {
    try { return { pending: userId ? (decode(raw, userId, factoryId)[slot] as SlotTypes[C] | undefined) ?? null : null, error: '' } }
    catch (reason) { return { pending: null, error: raw === '!unavailable' ? unavailable : recordError(reason) } }
  }, [raw, userId, factoryId, slot])
  function save(value: SlotTypes[C]): boolean { try { writePending(userId, factoryId, slot, value); setWriteError(''); return true } catch (reason) { setWriteError(reason instanceof Error ? reason.message : unavailable); return false } }
  function clear(value: SlotTypes[C]): boolean { try { writePending(userId, factoryId, slot, value, true); setWriteError(''); return true } catch (reason) { setWriteError(reason instanceof Error ? reason.message : unavailable); return false } }
  function archiveRetiredBusiness(): boolean { try { archiveRetired(userId, factoryId, raw); setWriteError(''); return true } catch (reason) { setWriteError(reason instanceof Error ? reason.message : unavailable); return false } }
  return { pending: state.pending, error: writeError || state.error, canArchiveRetiredBusiness: state.error === retiredBusiness, archiveRetiredBusiness, save, clear }
}
