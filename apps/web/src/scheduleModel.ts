/** Turns factory facts and plans into structures the board can draw directly. Pure functions, no DOM.
 *
 *  Three basic facts shape the algorithm here:
 *  1. Operations expand from the persistent source batches; old snapshots without batch records are split by order quantity.
 *  2. The planned interval [changeover_start, start_at) is changeover and [start_at, end_at) is production;
 *     when resuming, changeover_start/start_at keep the execution history and resume_* gives the remaining work.
 *  3. Actual execution must come from segments[], the real segment history; interruptions are the gaps between segments. */

import type {
  ActualExecution, Assignment, CalendarWindow, FactoryResource, FactoryWorker,
  Order, Publication, RouteStep, Snapshot, TimeSpan,
} from './contracts'
import type { OperationState, ShiftBand } from './palette'
import { parseMs } from './dayWindow'
import { stepName } from './copy'

export interface OperationIdentity {
  operationId: string
  orderId: string
  batchId: string
  batchIndex: number
  productId: string
  productName: string
  stepId: string
  operationCode: string
  stepName: string
  /** Batch quantity, i.e. the output this operation covers. */
  quantity: number
  /** Position in the product route dependencies, starting at 1. */
  sequence: number
  routeVersion: string
}

export interface Span { phase: 'setup' | 'production'; startMs: number; endMs: number }

export interface OperationRow {
  identity: OperationIdentity | null
  operationId: string
  resourceId: string
  workerId: string
  state: OperationState
  assignment: Assignment | null
  actual: ActualExecution | null
  planSpans: Span[]
  actualSpans: Span[]
  /** True when the actual interval has no segment records: only rough start/end, and the interface must say segments are missing. */
  actualApproximate: boolean
  plannedStartMs: number
  plannedEndMs: number
  /** Changed compared with the active plan; only computed when comparing a plan. */
  changed: boolean
}

export interface Band { kind: ShiftBand; startMs: number; endMs: number; reason?: string }

/** The interval one operation takes on the board: the union of plan and actual. */
export const rowStartMs = (row: OperationRow): number => {
  const spans = [...row.planSpans, ...row.actualSpans]
  return spans.length ? Math.min(...spans.map((span) => span.startMs)) : Number.NaN
}
export const rowEndMs = (row: OperationRow): number => {
  const spans = [...row.planSpans, ...row.actualSpans]
  return spans.length ? Math.max(...spans.map((span) => span.endMs)) : Number.NaN
}

/** Consecutive similar operations merged into one bar.
 *
 *  A real factory day can have hundreds of operations: one machine running dozens of batches of the same order and operation.
 *  Drawn batch by batch, each block is a few pixels wide, fits no text and shows nothing. A merged bar
 *  answers "which order and operation is this machine working on here, and how many batches", with the batch detail one click away.
 *
 *  Only operations with the same state, order and operation that are continuous in time (allowing changeover-sized gaps) merge,
 *  so different orders, operations or states never mix into one bar. */
export interface RunBar {
  key: string
  startMs: number
  endMs: number
  state: OperationState
  orderId: string
  operationCode: string
  stepName: string
  /** How many operations were merged. 1 means a single operation. */
  count: number
  operationIds: string[]
  changed: boolean
}

export function mergeRuns(rows: OperationRow[], toleranceMs = 120_000): RunBar[] {
  const ordered = [...rows]
    .filter((row) => Number.isFinite(rowStartMs(row)))
    .sort((left, right) => rowStartMs(left) - rowStartMs(right) || left.operationId.localeCompare(right.operationId))
  const runs: RunBar[] = []
  for (const row of ordered) {
    const startMs = rowStartMs(row)
    const endMs = rowEndMs(row)
    const orderId = row.identity?.orderId ?? ''
    const operationCode = row.identity?.operationCode ?? ''
    const last = runs[runs.length - 1]
    const joinable = last !== undefined
      && last.state === row.state
      && last.orderId === orderId
      && last.operationCode === operationCode
      && orderId !== ''
      && startMs <= last.endMs + toleranceMs
    if (joinable) {
      last.endMs = Math.max(last.endMs, endMs)
      last.count += 1
      last.operationIds.push(row.operationId)
      last.changed = last.changed || row.changed
      continue
    }
    runs.push({
      key: row.operationId,
      startMs,
      endMs,
      state: row.state,
      orderId,
      operationCode,
      stepName: row.identity?.stepName ?? '',
      count: 1,
      operationIds: [row.operationId],
      changed: row.changed,
    })
  }
  return runs
}

/** Overlapping operations in one lane go to different sub-rows. A machine or worker does one thing at a time,
 *  but grouped by order, several operations of one order can run on different machines at once. */
export function packRows(rows: { startMs: number; endMs: number }[]): number[] {
  const ends: number[] = []
  return rows.map((row) => {
    const start = Number.isFinite(row.startMs) ? row.startMs : Number.NEGATIVE_INFINITY
    const slot = ends.findIndex((end) => end <= start)
    const index = slot === -1 ? ends.length : slot
    ends[index] = Number.isFinite(row.endMs) ? row.endMs : Number.POSITIVE_INFINITY
    return index
  })
}

export interface Lane {
  id: string
  title: string
  subtitle: string
  statusValue: string
  statusLabel: string
  bands: Band[]
  /** True when the lane has no calendar data; the interface says "shifts not provided" instead of assuming hours. */
  calendarMissing: boolean
  rows: OperationRow[]
}

export type Grouping = 'resource' | 'worker' | 'order'

/** Topological order of the route dependencies. When they cannot be confirmed the original order is returned, never guessed. */
export function topologicalRoute(steps: RouteStep[]): RouteStep[] {
  const done = new Set<string>()
  const ordered: RouteStep[] = []
  while (ordered.length < steps.length) {
    const ready = steps.filter((step) => !done.has(step.step_id) && step.predecessors.every((id) => done.has(id)))
    if (!ready.length) return [...ordered, ...steps.filter((step) => !done.has(step.step_id))]
    for (const step of ready) { ordered.push(step); done.add(step.step_id) }
  }
  return ordered
}

/** Prefer persistent batch IDs and sizes, keep WIP moved to stock, and exclude cancelled unstarted batches.
 *  Only old snapshots without production_batches get batch IDs generated from order quantities in the old format. */
export function operationIndex(snapshot: Snapshot | null): Map<string, OperationIdentity> {
  const index = new Map<string, OperationIdentity>()
  if (!snapshot) return index
  const routesByProduct = new Map<string, RouteStep[]>()
  for (const step of snapshot.profile.routes) {
    const list = routesByProduct.get(step.product_id)
    if (list) list.push(step)
    else routesByProduct.set(step.product_id, [step])
  }
  if (snapshot.production_batches !== undefined && snapshot.production_batches !== null) {
    for (const batch of snapshot.production_batches) {
      if (batch.purpose === 'CANCELLED') continue
      const product = snapshot.profile.products.find(item => item.product_id === batch.product_id)
      if (!product) continue
      const route = topologicalRoute((routesByProduct.get(batch.product_id) ?? []).filter(step => (step.route_version ?? product.route_version) === batch.route_version))
      route.forEach((step, position) => {
        const operationId = `${batch.batch_id}-${step.operation_code}`
        index.set(operationId, {
          operationId, orderId: batch.order_id, batchId: batch.batch_id, batchIndex: batch.sequence,
          productId: batch.product_id, productName: product.name, stepId: step.step_id,
          operationCode: step.operation_code, stepName: stepName(step.name), quantity: batch.quantity,
          sequence: position + 1, routeVersion: batch.route_version,
        })
      })
    }
    return index
  }
  for (const order of snapshot.orders) {
    const product = snapshot.profile.products.find((item) => item.product_id === order.product_id)
    if (!product || product.batch_size <= 0 || order.quantity % product.batch_size) continue
    const route = topologicalRoute(routesByProduct.get(order.product_id) ?? [])
    const revision = String(order.split_revision).padStart(3, '0')
    for (let batchIndex = 1; batchIndex <= order.quantity / product.batch_size; batchIndex += 1) {
      const batchId = `${order.order_id}-R${revision}-B${String(batchIndex).padStart(3, '0')}`
      route.forEach((step, position) => {
        index.set(`${batchId}-${step.operation_code}`, {
          operationId: `${batchId}-${step.operation_code}`,
          orderId: order.order_id,
          batchId,
          batchIndex,
          productId: product.product_id,
          productName: product.name,
          stepId: step.step_id,
          operationCode: step.operation_code,
          stepName: stepName(step.name),
          quantity: product.batch_size,
          sequence: position + 1,
          routeVersion: product.route_version,
        })
      })
    }
  }
  return index
}

/** The display state of one operation on the board. The order of the checks is the business priority: problems before progress, progress before plan. */
export function operationState(
  actual: ActualExecution | null,
  assignment: Assignment | null,
  nowMs: number,
): OperationState {
  if (actual) {
    if (actual.state === 'BLOCKED') return actual.remaining_minutes == null ? 'UNCONFIRMED' : 'BLOCKED'
    if (actual.quality_state === 'FAILED') return 'BLOCKED'
    if (actual.state === 'COMPLETED') return actual.quality_state === 'PASSED' ? 'DONE' : 'UNJUDGED'
    if (actual.state === 'SETUP') return 'SETUP'
    return 'RUNNING'
  }
  if (assignment && Number.isFinite(nowMs) && parseMs(assignment.end_at) <= nowMs) return 'REPLAN'
  return 'PLANNED'
}

function positive(phase: Span['phase'], startMs: number, endMs: number): Span[] {
  return Number.isFinite(startMs) && Number.isFinite(endMs) && endMs > startMs ? [{ phase, startMs, endMs }] : []
}

/** Plan bar. When resuming, [start_at, resume_changeover_start) is execution history and the interruption gap, not plan. */
export function planSpans(assignment: Assignment): Span[] {
  const changeover = parseMs(assignment.changeover_start)
  const start = parseMs(assignment.start_at)
  const end = parseMs(assignment.end_at)
  if (assignment.resume_at && assignment.resume_changeover_start) {
    return [
      ...positive('setup', changeover, start),
      ...positive('setup', parseMs(assignment.resume_changeover_start), parseMs(assignment.resume_at)),
      ...positive('production', parseMs(assignment.resume_at), end),
    ]
  }
  return [...positive('setup', changeover, start), ...positive('production', start, end)]
}

/** Actual bar. With segment records it is drawn as recorded; without them only a rough interval marked approximate. */
export function actualSpans(actual: ActualExecution, clockMs: number): { spans: Span[]; approximate: boolean } {
  if (actual.segments?.length) {
    return {
      spans: actual.segments
        .map((segment) => ({
          phase: segment.phase === 'SETUP' ? ('setup' as const) : ('production' as const),
          startMs: parseMs(segment.start_at),
          endMs: parseMs(segment.end_at),
        }))
        .filter((span) => Number.isFinite(span.startMs) && Number.isFinite(span.endMs) && span.endMs > span.startMs),
      approximate: false,
    }
  }
  const start = parseMs(actual.changeover_start ?? actual.actual_start)
  const end = parseMs(actual.actual_end) || clockMs
  const phase: Span['phase'] = actual.actual_start ? 'production' : 'setup'
  return { spans: positive(phase, start, end), approximate: true }
}

export const overlaps = (span: { startMs: number; endMs: number }, startMs: number, endMs: number): boolean =>
  span.startMs < endMs && span.endMs > startMs

export function clip(spans: Span[], startMs: number, endMs: number): Span[] {
  return spans
    .filter((span) => overlaps(span, startMs, endMs))
    .map((span) => ({ phase: span.phase, startMs: Math.max(span.startMs, startMs), endMs: Math.min(span.endMs, endMs) }))
}

/** Lane background: fill non-working time first, then calendar windows, then explicit unavailable periods. */
export function laneBands(
  calendar: CalendarWindow[] | undefined,
  unavailable: TimeSpan[] | undefined,
  dayStartMs: number,
  dayEndMs: number,
  statusMask: { fromMs: number; reason: string } | null,
): Band[] {
  if (!calendar) return []
  const bands: Band[] = [{ kind: 'CLOSED', startMs: dayStartMs, endMs: dayEndMs }]
  for (const window of calendar) {
    const startMs = Math.max(parseMs(window.start_at), dayStartMs)
    const endMs = Math.min(parseMs(window.end_at), dayEndMs)
    if (endMs > startMs) bands.push({ kind: window.kind === 'OVERTIME' ? 'OVERTIME' : 'NORMAL', startMs, endMs })
  }
  for (const window of unavailable ?? []) {
    const startMs = Math.max(parseMs(window.start_at), dayStartMs)
    const endMs = Math.min(parseMs(window.end_at), dayEndMs)
    if (endMs > startMs) bands.push({ kind: 'UNAVAILABLE', startMs, endMs, reason: 'Explicit unavailable period' })
  }
  if (statusMask) {
    const startMs = Math.max(statusMask.fromMs, dayStartMs)
    if (dayEndMs > startMs) bands.push({ kind: 'UNAVAILABLE', startMs, endMs: dayEndMs, reason: statusMask.reason })
  }
  return bands
}

export interface BuildInput {
  snapshot: Snapshot | null
  assignments: Assignment[]
  /** The active plan to compare against; when given, operations that differ from it are marked. */
  baseline?: Assignment[] | null
  dayStartMs: number
  dayEndMs: number
  grouping: Grouping
}

export interface BoardModel {
  lanes: Lane[]
  rows: OperationRow[]
  summary: DaySummary
  /** Number of lanes that have work that day but no calendar. */
  lanesWithoutCalendar: number
}

export interface DaySummary {
  total: number
  byState: Record<OperationState, number>
  orders: number
  overtimeMinutes: number
  quantityDone: number
}

const emptyStates = (): Record<OperationState, number> => ({
  PLANNED: 0, SETUP: 0, RUNNING: 0, DONE: 0, UNJUDGED: 0, BLOCKED: 0, UNCONFIRMED: 0, REPLAN: 0,
})

function overtimeMinutes(spans: Span[], calendar: CalendarWindow[] | undefined): number {
  if (!calendar) return 0
  let minutes = 0
  for (const window of calendar.filter((item) => item.kind === 'OVERTIME')) {
    const windowStart = parseMs(window.start_at)
    const windowEnd = parseMs(window.end_at)
    for (const span of spans) {
      const startMs = Math.max(span.startMs, windowStart)
      const endMs = Math.min(span.endMs, windowEnd)
      if (endMs > startMs) minutes += Math.round((endMs - startMs) / 60000)
    }
  }
  return minutes
}

export function buildBoard(input: BuildInput): BoardModel {
  const { snapshot, assignments, dayStartMs, dayEndMs, grouping } = input
  const index = operationIndex(snapshot)
  const clockMs = snapshot ? parseMs(snapshot.snapshot_clock) : Number.NaN
  const actualById = new Map((snapshot?.actuals ?? []).map((actual) => [actual.operation_id, actual]))
  const assignmentById = new Map(assignments.map((item) => [item.operation_id, item]))
  const baselineById = input.baseline ? new Map(input.baseline.map((item) => [item.operation_id, item])) : null

  const rows: OperationRow[] = []
  for (const operationId of new Set([...assignmentById.keys(), ...actualById.keys()])) {
    const assignment = assignmentById.get(operationId) ?? null
    const actual = actualById.get(operationId) ?? null
    const plan = assignment ? planSpans(assignment) : []
    const real = actual ? actualSpans(actual, clockMs) : { spans: [], approximate: false }
    if (!plan.some((span) => overlaps(span, dayStartMs, dayEndMs))
      && !real.spans.some((span) => overlaps(span, dayStartMs, dayEndMs))) continue
    const baseline = baselineById?.get(operationId) ?? null
    rows.push({
      identity: index.get(operationId) ?? null,
      operationId,
      resourceId: actual?.resource_id ?? assignment?.resource_id ?? '',
      workerId: actual?.worker_id ?? assignment?.worker_id ?? '',
      state: operationState(actual, assignment, clockMs),
      assignment,
      actual,
      planSpans: plan,
      actualSpans: real.spans,
      actualApproximate: real.approximate,
      plannedStartMs: assignment ? parseMs(assignment.changeover_start) : Number.NaN,
      plannedEndMs: assignment ? parseMs(assignment.end_at) : Number.NaN,
      changed: Boolean(baselineById && assignment && (!baseline
        || baseline.start_at !== assignment.start_at
        || baseline.end_at !== assignment.end_at
        || baseline.resource_id !== assignment.resource_id
        || baseline.worker_id !== assignment.worker_id)),
    })
  }
  rows.sort((left, right) => (left.plannedStartMs || Number.MAX_SAFE_INTEGER) - (right.plannedStartMs || Number.MAX_SAFE_INTEGER)
    || left.operationId.localeCompare(right.operationId))

  const summary: DaySummary = { total: rows.length, byState: emptyStates(), orders: 0, overtimeMinutes: 0, quantityDone: 0 }
  const orders = new Set<string>()
  for (const row of rows) {
    summary.byState[row.state] += 1
    if (row.identity) orders.add(row.identity.orderId)
    if (row.state === 'DONE') summary.quantityDone += row.actual?.completed_quantity ?? 0
  }
  summary.orders = orders.size

  const lanes = buildLanes(snapshot, rows, grouping, dayStartMs, dayEndMs, clockMs)
  for (const lane of lanes) {
    const calendar = laneCalendar(snapshot, grouping, lane.id)
    for (const row of lane.rows) summary.overtimeMinutes += overtimeMinutes(clip(row.planSpans, dayStartMs, dayEndMs), calendar)
  }
  return { lanes, rows, summary, lanesWithoutCalendar: lanes.filter((lane) => lane.calendarMissing && lane.rows.length).length }
}

function laneCalendar(snapshot: Snapshot | null, grouping: Grouping, laneId: string): CalendarWindow[] | undefined {
  if (!snapshot) return undefined
  if (grouping === 'resource') return snapshot.resources.find((item) => item.resource_id === laneId)?.calendar
  if (grouping === 'worker') return snapshot.workers.find((item) => item.worker_id === laneId)?.calendar
  return undefined
}

function resourceLane(resource: FactoryResource, statusLabels: Record<string, string>): Omit<Lane, 'bands' | 'rows' | 'calendarMissing'> {
  return {
    id: resource.resource_id,
    title: resource.resource_id,
    subtitle: resource.resource_type,
    statusValue: resource.status,
    statusLabel: statusLabels[resource.status] ?? 'Status not confirmed',
  }
}

function workerLane(worker: FactoryWorker, statusLabels: Record<string, string>): Omit<Lane, 'bands' | 'rows' | 'calendarMissing'> {
  return {
    id: worker.worker_id,
    title: worker.worker_id,
    subtitle: worker.overtime_available ? 'Overtime eligible' : 'No overtime',
    statusValue: worker.status,
    statusLabel: statusLabels[worker.status] ?? 'Status not confirmed',
  }
}

const statusLabels: Record<string, string> = {
  AVAILABLE: 'Available', MAINTENANCE: 'Maintenance', DOWN: 'Down', ABSENT: 'Absent', UNKNOWN: 'Status not confirmed',
}

function buildLanes(
  snapshot: Snapshot | null,
  rows: OperationRow[],
  grouping: Grouping,
  dayStartMs: number,
  dayEndMs: number,
  clockMs: number,
): Lane[] {
  if (!snapshot) return []
  if (grouping === 'order') {
    const byOrder = new Map<string, OperationRow[]>()
    for (const row of rows) {
      const key = row.identity?.orderId ?? 'Unidentified order'
      const list = byOrder.get(key)
      if (list) list.push(row)
      else byOrder.set(key, [row])
    }
    return [...byOrder.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([orderId, list]) => {
        const order = snapshot.orders.find((item) => item.order_id === orderId)
        const product = snapshot.profile.products.find((item) => item.product_id === order?.product_id)
        return {
          id: orderId,
          title: orderId,
          subtitle: product?.name ?? 'Product not confirmed',
          statusValue: order?.status ?? 'UNKNOWN',
          statusLabel: order ? orderStatusLabels[order.status] ?? 'Status not confirmed' : 'Order not confirmed',
          bands: [],
          calendarMissing: false,
          rows: list,
        }
      })
  }
  const byLane = new Map<string, OperationRow[]>()
  for (const row of rows) {
    const key = grouping === 'resource' ? row.resourceId : row.workerId
    if (!key) continue
    const list = byLane.get(key)
    if (list) list.push(row)
    else byLane.set(key, [row])
  }
  const source: Omit<Lane, 'bands' | 'rows' | 'calendarMissing'>[] = grouping === 'resource'
    ? snapshot.resources.map((item) => resourceLane(item, statusLabels))
    : snapshot.workers.map((item) => workerLane(item, statusLabels))
  return source.map((lane) => {
    const entity: { calendar?: CalendarWindow[]; unavailable?: TimeSpan[]; status: string } = grouping === 'resource'
      ? snapshot.resources.find((item) => item.resource_id === lane.id)!
      : snapshot.workers.find((item) => item.worker_id === lane.id)!
    const unavailableNow = entity.status === 'DOWN' || entity.status === 'ABSENT' || entity.status === 'MAINTENANCE'
    return {
      ...lane,
      calendarMissing: entity.calendar === undefined,
      bands: laneBands(entity.calendar, entity.unavailable, dayStartMs, dayEndMs,
        unavailableNow && Number.isFinite(clockMs)
          ? { fromMs: clockMs, reason: `${lane.statusLabel}; recovery time not confirmed` }
          : null),
      rows: byLane.get(lane.id) ?? [],
    }
  })
}

const orderStatusLabels: Record<string, string> = {
  CONFIRMED: 'Confirmed', IN_PROGRESS: 'In production', COMPLETED: 'Completed', CANCELLED: 'Cancelled',
}

/** Each order's planned completion against its due date. Returns not confirmed when operations are not fully scheduled; never estimated. */
export interface OrderProjection { orderId: string; plannedEndMs: number; dueMs: number; late: boolean; hardDeadline: boolean; covered: boolean }

export function orderProjections(orders: Order[], assignments: Assignment[], index: Map<string, OperationIdentity>): OrderProjection[] {
  const byOrder = new Map<string, { end: number; count: number }>()
  for (const assignment of assignments) {
    const identity = index.get(assignment.operation_id)
    if (!identity) continue
    const found = byOrder.get(identity.orderId) ?? { end: 0, count: 0 }
    byOrder.set(identity.orderId, { end: Math.max(found.end, parseMs(assignment.end_at)), count: found.count + 1 })
  }
  const expected = new Map<string, number>()
  for (const identity of index.values()) expected.set(identity.orderId, (expected.get(identity.orderId) ?? 0) + 1)
  return orders.map((order) => {
    const found = byOrder.get(order.order_id)
    const covered = Boolean(found && found.count === expected.get(order.order_id))
    const dueMs = parseMs(order.due_at)
    return {
      orderId: order.order_id,
      plannedEndMs: covered ? found!.end : Number.NaN,
      dueMs,
      late: covered ? found!.end > dueMs : false,
      hardDeadline: order.hard_deadline,
      covered,
    }
  })
}

/** The plan behind the active plan. Only a release the execution source confirmed as effective counts as the active plan. */
export function activeCandidateId(publications: Publication[] | undefined): string {
  const active = (publications ?? [])
    .filter((record) => record.release.source_state === 'ACTIVE')
    .sort((left, right) => parseMs(right.release.committed_at) - parseMs(left.release.committed_at))[0]
  return active?.candidate_id ?? ''
}
