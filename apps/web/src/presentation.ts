import type { Snapshot } from './contracts'
import { operationIndex, topologicalRoute } from './scheduleModel'
import { metricNames, stepName } from './copy'

export function dateTime(value: string | null | undefined, timezone?: string): string {
  if (!value) return 'Not confirmed'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return 'Not confirmed'
  return new Intl.DateTimeFormat('en-CA', { timeZone: timezone ?? 'UTC', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(date).replace(',', '')
}

export const labels: Record<string, string> = {
  AVAILABLE: 'Available', MAINTENANCE: 'Maintenance', DOWN: 'Down', UNKNOWN: 'Not confirmed', ABSENT: 'Absent',
  SETUP: 'Changeover',
  CONFIRMED: 'Confirmed', IN_PROGRESS: 'In progress', COMPLETED: 'Completed', CANCELLED: 'Cancelled', BLOCKED: 'Blocked',
  EXPECTED: 'Expected', RECEIVED: 'Received', PENDING: 'Awaiting check', PASSED: 'Passed', FAILED: 'Failed',
  EA: 'pcs', SET: 'sets', GFU: 'grease fill units', minutes: 'min', operations: 'operations',
}
export const displayStatus = (value: string) => labels[value] ?? 'Not confirmed'

const checkerIssues: Record<string, string> = {
  INVALID_CONTRACT: 'The plan or factory data format is incomplete; check the input and recalculate.',
  INVALID_SCENARIO: 'The overtime scenario is not explicitly confirmed; check this scheduling setup.',
  NO_SOLUTION: 'The plan has no operation assignments and cannot pass the plan check.',
  UNSUPPORTED_WIP: 'There are started or completed records; check WIP and history first.',
  UNSUPPORTED_BASELINE: 'An execution plan already exists; check frozen operations and material reservations first.',
  UNSUPPORTED_ORDER_STATE: 'The order state does not fit initial scheduling; check the orders and actual execution records.',
  EMPTY_SCOPE: 'There are no confirmed production orders; sync and confirm demand first.',
  SOURCE_NOT_CURRENT: 'The factory data is incomplete or out of date; sync again and check the source.',
  FUTURE_ACTUAL_RECEIPT: 'An actual receipt time is later than the current factory time; check the receipt records.',
  VERSION_MISMATCH: 'The plan basis does not match the current data version; recalculate.',
  FACTORY_MISMATCH: 'The plan does not match the current factory; check which factory it belongs to.',
  STALE_TIME: 'The acceptance time of the plan has expired; recalculate from the current factory time.',
  UNSUPPORTED_SCENARIO: 'The plan contains scenario changes that are not allowed; check the scheduling setup.',
  MISSING_CONSENT: 'An overtime plan is missing the manager approval requirement and cannot be released.',
  UNSUPPORTED_CONSENT: 'The plan requests a permission the current scenario does not support; check the required approvals.',
  MISSING_OPERATION: 'The plan is missing operations; add them and recalculate.',
  EXTRA_OPERATION: 'The plan contains operations outside the demand scope; check the orders and routes.',
  WRONG_DURATION: 'An operation duration does not match setup time, cycle time and batch size; recalculate.',
  INVALID_TIME_GRID: 'An operation time is not on the whole-minute scheduling grid; recalculate.',
  OUTSIDE_PLANNING_WINDOW: 'A changeover or production run is outside the executable window; recalculate.',
  QUALITY_PRECEDENCE: 'The preceding quality check is not complete, so the next operation cannot start.',
  PRECEDENCE: 'The preceding operation is not complete, so the next operation cannot start.',
  UNKNOWN_ASSIGNMENT: 'The schedule references an unconfirmed machine or worker; check the resource data.',
  RESOURCE_QUALIFICATION: 'The machine is not qualified for this operation; reschedule.',
  WORKER_QUALIFICATION: 'The worker lacks the skill for this operation; reschedule.',
  RESOURCE_UNAVAILABLE: 'The machine is not confirmed available; check its down or maintenance status.',
  WORKER_UNAVAILABLE: 'The worker is not confirmed available; check attendance.',
  RESOURCE_CALENDAR: 'The changeover and production do not fit fully inside the machine calendar; reschedule.',
  WORKER_CALENDAR: 'The changeover and production do not fit fully inside the worker calendar; check shifts and overtime eligibility.',
  RESOURCE_UNAVAILABLE_INTERVAL: 'The changeover or production uses a period when the machine is unavailable; reschedule.',
  WORKER_UNAVAILABLE_INTERVAL: 'The changeover or production uses a period when the worker is unavailable; reschedule.',
  CHANGEOVER: 'The changeover time does not fit the adjacent products or does not directly precede the operation; recalculate.',
  RESOURCE_CONFLICT: 'Changeover or production on the machine overlaps; reschedule.',
  WORKER_CONFLICT: 'Changeover or production of the same worker overlaps; reschedule.',
  MATERIAL_SHORTAGE: 'Material is insufficient to kit the whole batch at start; check available stock, reservations and receipts.',
  HARD_DEADLINE: 'The planned order completion is past the hard deadline; this plan cannot be approved.',
  METRIC_MISMATCH: 'The plan metrics do not match the independent recalculation; recalculate.',
  INVALID_OBJECTIVE_BOUND: 'The objective lower bound exceeds the current plan value, so the optimality proof is invalid; recalculate.',
}

export function checkerIssueText(code: string, objectId?: string | null): string {
  const message = checkerIssues[code] ?? 'The plan check did not pass; recalculate and ask the planning owner to check.'
  const subject = objectId ? (code === 'INVALID_OBJECTIVE_BOUND' ? metricNames[objectId] ?? 'plan metric' : objectId) : null
  return subject ? `${subject}: ${message}` : message
}

export function routeNames(snapshot: Snapshot, productId: string): string {
  const steps = snapshot.profile.routes.filter((step) => step.product_id === productId)
  const ordered = topologicalRoute(steps)
  const resolvable = ordered.every((step, position) =>
    step.predecessors.every((id) => ordered.slice(0, position).some((earlier) => earlier.step_id === id)))
  if (!resolvable) return 'The route dependencies cannot be confirmed'
  return ordered.map((step) => `${step.operation_code} ${stepName(step.name)}`).join(' → ')
}

/** Operation identities are expanded in one place (scheduleModel.operationIndex); this only picks display names. */
export function operationNames(snapshot: Snapshot | null): Map<string, { orderId: string; batchId: string; name: string }> {
  const result = new Map<string, { orderId: string; batchId: string; name: string }>()
  for (const [operationId, identity] of operationIndex(snapshot)) {
    result.set(operationId, {
      orderId: identity.orderId,
      batchId: identity.batchId,
      name: `${identity.operationCode} ${identity.stepName}`,
    })
  }
  return result
}
