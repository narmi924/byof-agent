import type { BusinessStudyJob } from './businessContracts'
import { parseBusinessStudyJob } from './businessContracts'
export type Priority = 'delivery' | 'stability' | 'overtime'
export interface AssistantRequest { request_id: string; run_id: string; kind: 'start' | 'approve' | 'recover' | 'outage' | 'scenario' | 'preference' | 'reset' | 'business_accept' | 'treatment_execute'; payload: Record<string, unknown> }
export interface AssistantAction extends AssistantRequest { action_id: string; state: 'QUEUED' | 'DONE' | 'FAILED' | 'ATTENTION' | 'CANCELLED'; result: Record<string, unknown> | null; created_at: string }
export interface Learning { weights: Record<Priority, number>; samples: number; active: boolean; explicit: Priority | null; summary: string }
export interface MaterialShortfall { material_id: string; material_name: string; unit: string; unstarted_demand: number; unreserved_on_hand: number; confirmed_inbound: number; minimum_shortfall: number }
export interface MaterialBalance { snapshot_id: string | null; shortfalls: MaterialShortfall[] }
export interface RecoveryStep { owner: 'Manager' | 'Shop floor' | 'Agent'; action: string }
export interface RecoveryPath { path_id: string; kind: string; title: string; evidence: string; steps: RecoveryStep[]; prompt: string; tracking_case_id?: string | null }
export interface RecoveryRequest { action_id: string; created_at: string; path: RecoveryPath; state: 'AWAITING_SOURCE' | 'SOURCE_CHANGED' | 'RECHECKING' | 'AWAITING_APPROVAL' | 'NEEDS_ATTENTION' | 'RESOLVED' | 'CANCELLED'; case_id?: string | undefined; next_action?: string | undefined; condition_remaining?: boolean }
export interface AssistantState { actions: AssistantAction[]; learning: Learning; material_balance: MaterialBalance; business_studies?: BusinessStudyJob[]; recovery_paths?: RecoveryPath[] }
export interface AssistantAttempt { kind: 'action' | 'chat' | 'response' | 'stop' | 'execution'; body: Record<string, unknown>; caseId: string | null; taskId: string | null }

const object = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
export function parseRecoveryPath(v: unknown): RecoveryPath {
  if (!object(v) || !['path_id', 'kind', 'title', 'evidence', 'prompt'].every(key => typeof v[key] === 'string' && v[key])
    || (v.tracking_case_id !== undefined && v.tracking_case_id !== null && (typeof v.tracking_case_id !== 'string' || !v.tracking_case_id))
    || !Array.isArray(v.steps) || !v.steps.length || !v.steps.every(step => object(step)
      && ['Manager', 'Shop floor', 'Agent'].includes(String(step.owner)) && typeof step.action === 'string' && step.action)) throw new Error('The recovery path is not recognized.')
  return v as unknown as RecoveryPath
}
export function parseRecoveryRequests(v: unknown): { run_id: string | null; requests: RecoveryRequest[] } {
  if (!object(v) || (v.run_id !== null && typeof v.run_id !== 'string') || !Array.isArray(v.requests)) throw new Error('The shop floor recovery case is not recognized.')
  return { run_id: v.run_id as string | null, requests: v.requests.map(item => {
    if (!object(item) || typeof item.action_id !== 'string' || !item.action_id || typeof item.created_at !== 'string'
      || !Number.isFinite(Date.parse(item.created_at)) || !['AWAITING_SOURCE', 'SOURCE_CHANGED', 'RECHECKING', 'AWAITING_APPROVAL', 'NEEDS_ATTENTION', 'RESOLVED', 'CANCELLED'].includes(String(item.state))) throw new Error('The shop floor recovery case is not recognized.')
    return { case_id: typeof item.case_id === 'string' ? item.case_id : undefined, next_action: typeof item.next_action === 'string' ? item.next_action : undefined, action_id: item.action_id, created_at: item.created_at, state: item.state as RecoveryRequest['state'], path: parseRecoveryPath(item.path) }
  }) }
}
export function parseAssistantAction(v: unknown): AssistantAction {
  if (!object(v) || !['action_id', 'request_id', 'run_id', 'created_at'].every(k => typeof v[k] === 'string' && v[k])
    || !['start', 'approve', 'recover', 'outage', 'scenario', 'preference', 'reset', 'business_accept', 'treatment_execute'].includes(String(v.kind))
    || !['QUEUED', 'DONE', 'FAILED', 'ATTENTION', 'CANCELLED'].includes(String(v.state)) || !object(v.payload)
    || (v.result !== null && !object(v.result)) || !Number.isFinite(Date.parse(String(v.created_at)))) throw new Error('The assistant action record is not recognized; reload it.')
  return v as unknown as AssistantAction
}
export function parseAssistantState(v: unknown): AssistantState {
  if (!object(v) || !Array.isArray(v.actions) || !object(v.learning) || !object(v.material_balance)) throw new Error('The assistant state is not recognized.')
  const l = v.learning
  const balance = v.material_balance
  if ((balance.snapshot_id !== null && typeof balance.snapshot_id !== 'string') || !Array.isArray(balance.shortfalls)
    || !balance.shortfalls.every(item => object(item)
      && ['material_id', 'material_name', 'unit'].every(key => typeof item[key] === 'string')
      && ['unstarted_demand', 'unreserved_on_hand', 'confirmed_inbound', 'minimum_shortfall'].every(key => Number.isSafeInteger(item[key]) && Number(item[key]) >= 0))) throw new Error('The material balance is not recognized.')
  if (!object(l.weights) || !['delivery', 'stability', 'overtime'].every(k => typeof (l.weights as Record<string, unknown>)[k] === 'number' && Number.isFinite((l.weights as Record<string, number>)[k]) && (l.weights as Record<string, number>)[k]! >= 0)
    || !Number.isSafeInteger(l.samples) || Number(l.samples) < 0 || typeof l.active !== 'boolean' || typeof l.summary !== 'string'
    || (l.explicit !== null && !['delivery', 'stability', 'overtime'].includes(String(l.explicit)))) throw new Error('The learned preference is not recognized.')
  if (v.business_studies !== undefined && !Array.isArray(v.business_studies)) throw new Error('The option comparison records are not recognized.')
  if (v.recovery_paths !== undefined && !Array.isArray(v.recovery_paths)) throw new Error('The recovery path is not recognized.')
  return { business_studies: (v.business_studies as unknown[] | undefined ?? []).map(parseBusinessStudyJob), recovery_paths: (v.recovery_paths as unknown[] | undefined ?? []).map(parseRecoveryPath), actions: v.actions.map(parseAssistantAction), learning: l as unknown as Learning, material_balance: balance as unknown as MaterialBalance }
}
