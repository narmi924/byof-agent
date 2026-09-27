import { parseAgentRun, parseAgentRuns, parseApproval, parseFactories, parsePlanExport, parseRelease, parseSimulatorStatus, parseWorkspace } from './contracts'
import type { ApprovalScope, ReplayRequest, SimulatorCommand, TodayRunRequest } from './contracts'
import { parseCase, parseCaseDetail, parseCases, parseHumanTask, parseHumanTasks } from './caseContracts'
import { parseRiskSuggestions } from './riskContracts'
import { parseModelSelection } from './modelContracts'

export async function readModelSelection(signal: AbortSignal) {
  return parseModelSelection(await request('/api/model-selection', signal))
}

export async function changeModelSelection(model_id: string, expected_version: number, request_id: string, signal: AbortSignal) {
  return parseModelSelection(await request('/api/model-selection', signal, { model_id, expected_version, request_id }))
}
import { normalizeContactEmail, parseContact, parseContacts, parseNotifications } from './notificationContracts'
import type { ContactInput } from './notificationContracts'
import { parseEffectiveObjective, parsePreferenceConfirmation, parsePreferenceDeactivation, parsePreferenceProposal, parsePreferenceRejection, parsePreferenceState } from './preferenceContracts'
import type { ObjectiveDefinition, PreferenceHead, PreferenceProposal, ProposalInput } from './preferenceContracts'
import { parseValidationCertificate } from './revalidationContracts'
import { parseAssistantAction, parseAssistantState, parseRecoveryRequests } from './assistantContracts'

export interface SystemStatus {
  name: 'BYOF'
  version: string
  state: 'setup_required' | 'ready'
  message: string
}

export interface Connection {
  system: SystemStatus
  session: { user_id?: string; username: string; grants: { factory_id: string; role: string }[] } | null
}

export const entryRole = () => window.location.pathname.startsWith('/factory') || window.location.pathname.startsWith('/simulator') ? 'maintainer' as const : 'manager' as const
const surfaceHeaders = () => ({ 'X-BYOF-Surface': entryRole() === 'manager' ? 'agent' : 'simulator' })

let csrfToken: string | null = null

export async function readAssistant(factoryId: string, signal: AbortSignal, caseId?: string) {
  const query = caseId ? `?${new URLSearchParams({ case_id: caseId })}` : ''
  return parseAssistantState(await request(`/api/factories/${encodeURIComponent(factoryId)}/assistant${query}`, signal))
}
export async function readRecoveryRequests(factoryId: string, signal: AbortSignal) {
  return parseRecoveryRequests(await request(`/api/factories/${encodeURIComponent(factoryId)}/assistant/recovery-requests`, signal))
}
export async function submitAssistant(factoryId: string, body: Record<string, unknown>, signal: AbortSignal) {
  return parseAssistantAction(await request(`/api/factories/${encodeURIComponent(factoryId)}/assistant/actions`, signal, body))
}
export async function decideExecution(factoryId: string, actionId: string, body: Record<string, unknown>, signal: AbortSignal) {
  const result = parseAssistantAction(await request(`/api/factories/${encodeURIComponent(factoryId)}/assistant/executions/${encodeURIComponent(actionId)}/decision`, signal, body))
  if (result.action_id !== actionId) throw new Error('The execution receipt does not match the current decision; check the original request.')
  return result
}

export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly code: string) { super(message) }
}

export function definitiveRejection(reason: unknown, recovering: boolean): boolean {
  return reason instanceof ApiError && [400, 404, 409, 422].includes(reason.status)
    && reason.code !== 'CASE_CLOSED' && !(recovering && reason.status === 409)
}

async function request(path: string, signal: AbortSignal, body?: unknown, csrf = true): Promise<unknown> {
  if (body !== undefined && csrf && !csrfToken) throw new Error('Page verification is not complete; reconnect before acting.')
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST', signal, credentials: 'same-origin', cache: 'no-store',
    headers: { ...surfaceHeaders(), Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json', ...(csrf && csrfToken ? { 'X-CSRF-Token': csrfToken } : {}) }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  if (!response.ok) {
    if (response.status === 401) csrfToken = null
    let message = response.status === 401 ? 'Your sign-in has expired; sign in again.' : response.status === 403 ? 'This account is not allowed to do this.' : 'The request did not complete; refresh shortly to check.'
    let code = 'REQUEST_FAILED'
    try {
      const error: unknown = await response.json()
      if (typeof error === 'object' && error !== null && 'code' in error && typeof error.code === 'string'
        && 'message' in error && typeof error.message === 'string' && error.message.length <= 1000) {
        code = error.code
        message = error.message
      }
    } catch { /* A failed proxy response may contain no JSON. */ }
    throw new ApiError(message, response.status, code)
  }
  return response.json()
}

function readToken(value: unknown): string {
  if (typeof value !== 'object' || value === null || !('csrf_token' in value)
    || typeof value.csrf_token !== 'string' || !value.csrf_token) {
    throw new Error('Page verification could not be confirmed; reconnect.')
  }
  return value.csrf_token
}

function isSystemStatus(value: unknown): value is SystemStatus {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return record.name === 'BYOF'
    && typeof record.version === 'string' && record.version.length > 0
    && (record.state === 'setup_required' || record.state === 'ready')
    && typeof record.message === 'string'
    && record.message.trim().length > 0 && record.message.length <= 1000
}

async function readSystem(signal: AbortSignal): Promise<SystemStatus> {
  const response = await fetch('/api/system/status', {
    signal, credentials: 'same-origin', headers: { ...surfaceHeaders(), Accept: 'application/json' },
    cache: 'no-store',
  })
  if (!response.ok) throw new Error('Could not get the system status; reconnect shortly.')
  const body: unknown = await response.json()
  if (!isSystemStatus(body)) throw new Error('The system status is not recognized; ask the administrator to check the service configuration.')
  return body
}

async function readSession(signal: AbortSignal): Promise<Connection['session']> {
  const response = await fetch('/api/session', {
    signal, credentials: 'same-origin', headers: { ...surfaceHeaders(), Accept: 'application/json' },
    cache: 'no-store',
  })
  if (response.status === 401) { csrfToken = null; return null }
  if (!response.ok) throw new Error('Could not get the sign-in status; reconnect. If it still fails, contact the administrator.')
  const body: unknown = await response.json()
  if (typeof body !== 'object' || body === null || !('username' in body)
    || typeof body.username !== 'string' || !body.username.trim()
    || !('grants' in body) || !Array.isArray(body.grants)
    || !body.grants.every((grant: unknown) => typeof grant === 'object' && grant !== null
      && 'factory_id' in grant && typeof grant.factory_id === 'string'
      && 'role' in grant && typeof grant.role === 'string')) {
    throw new Error('The signed-in identity could not be confirmed; sign in again.')
  }
  if ('user_id' in body && (typeof body.user_id !== 'string' || !body.user_id)) throw new Error('The signed-in identity could not be confirmed; sign in again.')
  return { username: body.username, grants: body.grants, ...('user_id' in body && typeof body.user_id === 'string' ? { user_id: body.user_id } : {}) }
}

export async function readConnection(signal: AbortSignal): Promise<Connection> {
  const [system, session] = await Promise.all([readSystem(signal), readSession(signal)])
  if (session) csrfToken = readToken(await request('/api/csrf', signal, {}, false))
  return { system, session }
}

export async function signIn(role: 'manager' | 'maintainer', signal: AbortSignal): Promise<void> {
  const response = await fetch('/api/role-session', {
    method: 'POST', signal, credentials: 'same-origin', cache: 'no-store',
    headers: { ...surfaceHeaders(), 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ role }),
  })
  if (response.status === 403) throw new Error('The role selection was not allowed; open the workbench address again.')
  if (response.status === 422) throw new Error('Choose the manager or the disruption simulator.')
  if (response.status === 503) throw new Error('The demo identities are not set up yet; run the team setup.')
  if (!response.ok) throw new Error('The sign-in service is unavailable; try again shortly.')
  const body: unknown = await response.json()
  if (typeof body !== 'object' || body === null || !('csrf_token' in body)
    || typeof body.csrf_token !== 'string' || !body.csrf_token) {
    throw new Error('The sign-in result could not be confirmed; reconnect to check the sign-in status.')
  }
  csrfToken = body.csrf_token
}

export async function signOut(signal: AbortSignal): Promise<void> {
  const value = await request('/api/logout', signal, {})
  if (typeof value !== 'object' || value === null || !('status' in value) || value.status !== 'signed_out') {
    throw new Error('The sign-out result could not be confirmed; reconnect to check the sign-in status.')
  }
  csrfToken = null
}

const factoryPath = (factoryId: string) => `/api/factories/${encodeURIComponent(factoryId)}`
export async function readFactories(signal: AbortSignal) { return parseFactories(await request('/api/factories', signal)) }
export async function readWorkspace(factoryId: string, signal: AbortSignal, scope?: { caseId?: string | undefined; candidateId?: string | undefined }) {
  const query = new URLSearchParams()
  if (scope?.caseId) query.set('case_id', scope.caseId)
  if (scope?.candidateId) query.set('candidate_id', scope.candidateId)
  return parseWorkspace(await request(`${factoryPath(factoryId)}/workspace${query.size ? `?${query}` : ''}`, signal), factoryId)
}
export async function syncFactory(factoryId: string, signal: AbortSignal) {
  const value = await request(`${factoryPath(factoryId)}/sync`, signal, {})
  if (typeof value !== 'object' || value === null || !('snapshot_id' in value) || typeof value.snapshot_id !== 'string'
    || !('content_hash' in value) || typeof value.content_hash !== 'string') {
    throw new Error('The sync result could not be confirmed; refresh to check the source and time.')
  }
}
export async function solveFactory(factoryId: string, requestId: string, allowOvertime: boolean, signal: AbortSignal, newActionsNotBefore: string | null = null) {
  const value = await request(`${factoryPath(factoryId)}/solve`, signal, { request_id: requestId, allow_overtime: allowOvertime, time_limit: 30, ...(newActionsNotBefore !== null ? { new_actions_not_before: newActionsNotBefore } : {}) })
  if (typeof value !== 'object' || value === null || !('job_id' in value) || typeof value.job_id !== 'string') {
    throw new Error('The scheduling request result could not be confirmed; refresh to check the job records.')
  }
}
export async function approveCandidate(factoryId: string, candidateId: string, requestId: string, candidateHash: string, scope: ApprovalScope, decision: 'APPROVED' | 'REJECTED', signal: AbortSignal) {
  const approval = parseApproval(await request(`${factoryPath(factoryId)}/candidates/${encodeURIComponent(candidateId)}/approvals`, signal,
    { request_id: requestId, candidate_hash: candidateHash, action_scope: scope, decision }))
  if (approval.candidate_hash !== candidateHash || approval.action_scope !== scope || approval.decision !== decision) {
    throw new Error('The returned decision does not match the current plan; refresh to check the records.')
  }
  return approval
}
export async function approveCandidateProgress(factoryId: string, candidateId: string, requestId: string, candidateHash: string, scope: ApprovalScope, signal: AbortSignal) {
  const approval = parseApproval(await request(`${factoryPath(factoryId)}/candidates/${encodeURIComponent(candidateId)}/progress-approvals`, signal,
    { request_id: requestId, candidate_hash: candidateHash, action_scope: scope, decision: 'APPROVED' }))
  if (approval.candidate_hash !== candidateHash || approval.action_scope !== scope || approval.decision !== 'APPROVED') throw new Error('The approval result does not match the original request; check the records.')
  return approval
}

export async function readAgentRuns(factoryId: string, signal: AbortSignal) { return parseAgentRuns(await request(`${factoryPath(factoryId)}/agent-runs`, signal)) }
export async function startAgentRun(factoryId: string, requestId: string, message: string, signal: AbortSignal) {
  return parseAgentRun(await request(`${factoryPath(factoryId)}/agent-runs`, signal, { request_id: requestId, message }))
}

export async function publishCandidate(factoryId: string, candidateId: string, requestId: string, candidateHash: string, signal: AbortSignal, certificateId?: string) {
  const release = parseRelease(await request(`${factoryPath(factoryId)}/candidates/${encodeURIComponent(candidateId)}/publications`, signal, { request_id: requestId, candidate_hash: candidateHash, ...(certificateId ? { certificate_id: certificateId } : {}) }), factoryId)
  if (release.candidate_hash !== candidateHash) throw new Error('The release record does not match the current plan; refresh to check.')
  return release
}
export async function exportCandidate(factoryId: string, candidateId: string, candidateHash: string, signal: AbortSignal): Promise<Blob> {
  const value = parsePlanExport(await request(`${factoryPath(factoryId)}/candidates/${encodeURIComponent(candidateId)}/export?${new URLSearchParams({ candidate_hash: candidateHash })}`, signal), factoryId, candidateId, candidateHash)
  return new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json;charset=utf-8' })
}
export async function validateCandidate(factoryId: string, candidateId: string, requestId: string, candidateHash: string, signal: AbortSignal) {
  const certificate = parseValidationCertificate(await request(`${factoryPath(factoryId)}/candidates/${encodeURIComponent(candidateId)}/validations`, signal, { request_id: requestId, candidate_hash: candidateHash }), factoryId)
  if (certificate.candidate_hash !== candidateHash) throw new Error('The check certificate does not match the original plan; check the original request.')
  return certificate
}

const simulatorPath = (factoryId: string) => `/api/admin/factories/${encodeURIComponent(factoryId)}/simulator`
export async function readSimulator(factoryId: string, signal: AbortSignal) {
  return parseSimulatorStatus(await request(simulatorPath(factoryId), signal), factoryId)
}
export async function commandSimulator(factoryId: string, command: SimulatorCommand, signal: AbortSignal) {
  const result = await request(`${simulatorPath(factoryId)}/commands`, signal, command)
  if (typeof result !== 'object' || result === null || !('factory_id' in result) || result.factory_id !== factoryId
    || !('run_id' in result) || result.run_id !== command.run_id || !('request_id' in result) || result.request_id !== command.request_id
    || !('snapshot_hash' in result) || typeof result.snapshot_hash !== 'string' || !result.snapshot_hash) {
    throw new Error('The action receipt could not be confirmed; check the result of the same request.')
  }
}
export async function replaySimulator(factoryId: string, command: ReplayRequest, signal: AbortSignal) {
  const result = await request(`${simulatorPath(factoryId)}/replays`, signal, command)
  if (typeof result !== 'object' || result === null || !('factory_id' in result) || result.factory_id !== factoryId
    || !('request_id' in result) || result.request_id !== command.request_id
    || !('origin_run_id' in result) || result.origin_run_id !== command.expected_run_id
    || !('run_id' in result) || typeof result.run_id !== 'string' || !result.run_id || result.run_id === command.expected_run_id) {
    throw new Error('The replay creation result could not be confirmed; check the original request.')
  }
}

export async function todayRunSimulator(factoryId: string, command: TodayRunRequest, signal: AbortSignal) {
  const result = await request(`${simulatorPath(factoryId)}/today-runs`, signal, command)
  if (typeof result !== 'object' || result === null || !('factory_id' in result) || result.factory_id !== factoryId
    || !('request_id' in result) || result.request_id !== command.request_id
    || !('origin_run_id' in result) || result.origin_run_id !== command.expected_run_id
    || !('run_id' in result) || typeof result.run_id !== 'string' || !result.run_id || result.run_id === command.expected_run_id
    || !('business_clock' in result) || typeof result.business_clock !== 'string' || !Number.isFinite(Date.parse(result.business_clock))) {
    throw new Error('The new run creation result could not be confirmed; check the original request.')
  }
}

export async function readCases(factoryId: string, signal: AbortSignal) {
  return parseCases(await request(`${factoryPath(factoryId)}/cases`, signal), factoryId)
}
export async function readRiskSuggestions(factoryId: string, runId: string | undefined, signal: AbortSignal) {
  return parseRiskSuggestions(await request(`${factoryPath(factoryId)}/risk-suggestions`, signal), runId)
}
export async function readCase(factoryId: string, caseId: string, signal: AbortSignal) {
  return parseCaseDetail(await request(`${factoryPath(factoryId)}/cases/${encodeURIComponent(caseId)}`, signal), factoryId, caseId)
}
export async function readCaseHistory(factoryId: string, caseId: string, cursor: { at: string; id: string }, signal: AbortSignal) {
  const query = new URLSearchParams({ before_at: cursor.at, before_id: cursor.id })
  return parseCaseDetail(await request(`${factoryPath(factoryId)}/cases/${encodeURIComponent(caseId)}/history?${query}`, signal), factoryId, caseId)
}
export async function readHumanTasks(factoryId: string, signal: AbortSignal) {
  return parseHumanTasks(await request(`${factoryPath(factoryId)}/human-tasks`, signal), factoryId)
}
export async function readHumanTask(factoryId: string, taskId: string, signal: AbortSignal) {
  const task = parseHumanTask(await request(`${factoryPath(factoryId)}/human-tasks/${encodeURIComponent(taskId)}`, signal), factoryId)
  if (task.task_id !== taskId) throw new Error('The task link does not match the returned data; check the link.')
  return task
}
export async function readNotificationContacts(factoryId: string, signal: AbortSignal) {
  return parseContacts(await request(`/api/admin/factories/${encodeURIComponent(factoryId)}/notification-contacts`, signal))
}
export async function saveNotificationContact(factoryId: string, body: ContactInput, signal: AbortSignal) {
  const contact = parseContact(await request(`/api/admin/factories/${encodeURIComponent(factoryId)}/notification-contacts`, signal, body))
  if (contact.role !== body.role || contact.user_id !== body.user_id || contact.email !== normalizeContactEmail(body.email) || contact.enabled !== body.enabled) throw new Error('The saved contact does not match these settings; check the original request.')
  return contact
}
export async function readNotifications(factoryId: string, taskId: string, signal: AbortSignal) {
  return parseNotifications(await request(`${factoryPath(factoryId)}/notifications?${new URLSearchParams({ task_id: taskId })}`, signal), taskId)
}
export async function readPreferences(factoryId: string, signal: AbortSignal) {
  return parsePreferenceState(await request(`${factoryPath(factoryId)}/preferences`, signal), factoryId)
}
export async function proposePreference(factoryId: string, body: ProposalInput, signal: AbortSignal) {
  const proposal = parsePreferenceProposal(await request(`${factoryPath(factoryId)}/preferences/proposals`, signal, body))
  if (proposal.scope_type !== body.scope_type || proposal.scope_id !== body.scope_id || proposal.expected_version !== body.expected_version || proposal.definition.selection !== body.definition.selection || proposal.definition.objective_order.join() !== body.definition.objective_order.join() || proposal.definition.max_weighted_tardiness !== body.definition.max_weighted_tardiness || proposal.definition.max_incremental_overtime_minutes !== body.definition.max_incremental_overtime_minutes) throw new Error('The saved preference proposal does not match this input; check the original request.')
  return proposal
}
export async function confirmPreference(factoryId: string, proposal: PreferenceProposal, body: { request_id: string; expected_state_version: number }, signal: AbortSignal) {
  return parsePreferenceConfirmation(await request(`${factoryPath(factoryId)}/preferences/proposals/${encodeURIComponent(proposal.proposal_id)}/confirmations`, signal, body), proposal)
}
export async function rejectPreference(factoryId: string, proposalId: string, body: { request_id: string; reason: string }, signal: AbortSignal) {
  return parsePreferenceRejection(await request(`${factoryPath(factoryId)}/preferences/proposals/${encodeURIComponent(proposalId)}/rejections`, signal, body), proposalId)
}
export async function coordinatePreferences(factoryId: string, body: { request_id: string; expected_state_version: number; context_hash: string; definition: ObjectiveDefinition; reason: string }, signal: AbortSignal) {
  const result = parseEffectiveObjective(await request(`${factoryPath(factoryId)}/preferences/coordination`, signal, body))
  if (result.status !== 'READY' || result.definition?.selection !== body.definition.selection || result.definition.objective_order.join() !== body.definition.objective_order.join() || result.definition.max_weighted_tardiness !== body.definition.max_weighted_tardiness || result.definition.max_incremental_overtime_minutes !== body.definition.max_incremental_overtime_minutes || (result.coordination && (result.coordination.context_hash !== body.context_hash || result.coordination.reason !== body.reason))) throw new Error('The coordination receipt does not match the original decision; check the original request.')
  return result
}
export async function deactivatePreference(factoryId: string, head: PreferenceHead, body: { request_id: string; expected_state_version: number; expected_version: number; reason: string }, signal: AbortSignal) {
  return parsePreferenceDeactivation(await request(`${factoryPath(factoryId)}/preferences/${head.scope_type}/${encodeURIComponent(head.scope_id)}/deactivations`, signal, body), head)
}
export async function postCaseMessage(factoryId: string, caseId: string | null, body: Record<string, unknown>, signal: AbortSignal) {
  const record = parseCase(await request(`${factoryPath(factoryId)}/cases${caseId ? `/${encodeURIComponent(caseId)}/messages` : ''}`, signal, body), factoryId)
  if (caseId && record.case_id !== caseId) throw new Error('The returned case does not match this information; check the original request.')
  return record
}
export async function stopCaseTurn(factoryId: string, caseId: string, body: { request_id: string; expected_target: string }, signal: AbortSignal) {
  const record = parseCase(await request(`${factoryPath(factoryId)}/cases/${encodeURIComponent(caseId)}/stop`, signal, body), factoryId)
  if (record.case_id !== caseId) throw new Error('The stop receipt does not match the current conversation; refresh to check.')
  return record
}
export async function actOnHumanTask(factoryId: string, taskId: string, action: 'responses' | 'transfers' | 'cancellations' | 'handoffs', body: Record<string, unknown>, signal: AbortSignal) {
  const task = parseHumanTask(await request(`${factoryPath(factoryId)}/human-tasks/${encodeURIComponent(taskId)}/${action}`, signal, body), factoryId)
  if (task.task_id !== taskId) throw new Error('The returned task does not match this action; check the original request.')
  return task
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && !(error instanceof TypeError) && !(error instanceof SyntaxError) ? error.message : fallback
}
