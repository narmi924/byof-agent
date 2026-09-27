/** View state lives in the address query so it can be shared and restored on refresh, and old task links from emails keep working.
 *  A link only decides what is shown and grants no permission; the server still checks permissions by account role. */

import { workbenchLink } from './deepLinks'
import type { Grouping } from './scheduleModel'
import { isDayKey } from './dayWindow'

export const viewNames = ['chat', 'board', 'records', 'inbox', 'plan', 'facts', 'cases', 'settings', 'simulator'] as const
export type ViewName = typeof viewNames[number]
export const agentViews: ViewName[] = ['chat', 'board', 'records']
export const factoryViews: ViewName[] = ['facts', 'inbox', 'plan', 'cases', 'settings', 'simulator']

const paths: Record<ViewName, string> = {
  chat: '/agent/chat', board: '/agent/timeline', records: '/agent/records', inbox: '/factory/inbox',
  plan: '/factory/plan', facts: '/factory/facts', cases: '/factory/cases',
  settings: '/factory/settings', simulator: '/factory/simulator',
}

export function viewPath(view: ViewName): string { return paths[view] }

export const viewTitles: Record<ViewName, string> = {
  chat: 'Agent conversation',
  records: 'Execution log',
  board: 'Production timeline',
  inbox: 'Tasks',
  plan: 'Plan',
  facts: 'Factory data',
  cases: 'Cases',
  settings: 'Preferences and notifications',
  simulator: 'Disruption simulator',
}

export interface ViewState {
  view: ViewName
  factoryId: string
  day: string
  group: Grouping
  operationId: string
  candidateId: string
  caseId: string
  taskId: string
  /** Explanation shown when the link itself is broken; the interface shows it with a way back. */
  error: string
}

const identifier = /^\S{1,160}$/u
const groupings: Grouping[] = ['resource', 'worker', 'order']

function single(parameters: URLSearchParams, key: string): string {
  if (parameters.getAll(key).length > 1) return ''
  const value = parameters.get(key) ?? ''
  return identifier.test(value) ? value : ''
}

export function readViewState(search: string, pathname = '/'): ViewState {
  const link = workbenchLink(search)
  const parameters = new URLSearchParams(search)
  const view = parameters.get('view')
  const pathView = viewNames.find((name) => paths[name] === pathname)
  const group = parameters.get('group')
  const day = single(parameters, 'day')
  return {
    // The path decides the product area; old ?view= links still open and the workbench rewrites them to the canonical path.
    view: pathView ?? (pathname.startsWith('/agent/') ? 'chat'
      : pathname.startsWith('/factory/') ? 'facts'
        : viewNames.find((name) => name === view) ?? (link.caseId ? 'cases' : 'chat')),
    factoryId: link.factoryId,
    day: day && isDayKey(day) ? day : '',
    group: groupings.find((name) => name === group) ?? 'resource',
    operationId: single(parameters, 'operation'),
    candidateId: single(parameters, 'candidate'),
    caseId: link.caseId,
    taskId: link.taskId,
    error: link.error,
  }
}

/** Only writes parameters that differ from the defaults so the address stays readable. */
export function viewSearch(state: ViewState): string {
  const parameters = new URLSearchParams()
  if (state.factoryId) parameters.set('factory_id', state.factoryId)
  if (state.view === 'board') {
    if (state.day) parameters.set('day', state.day)
    if (state.group !== 'resource') parameters.set('group', state.group)
    if (state.operationId) parameters.set('operation', state.operationId)
    if (state.candidateId) parameters.set('candidate', state.candidateId)
  }
  if (state.view === 'plan' && state.candidateId) parameters.set('candidate', state.candidateId)
  // Case and task IDs keep the full form of email links; without the parent ID they are left out to avoid unparseable links.
  if (state.view === 'cases' && state.factoryId && state.caseId) parameters.set('case_id', state.caseId)
  if (state.view === 'cases' && state.factoryId && state.caseId && state.taskId) parameters.set('task_id', state.taskId)
  const search = parameters.toString()
  return search ? `?${search}` : ''
}

export function viewUrl(state: ViewState): string { return viewPath(state.view) + viewSearch(state) }
