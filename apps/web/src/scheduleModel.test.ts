import { describe, expect, it } from 'vitest'
import { dayBounds } from './dayWindow'
import {
  actualSpans, activeCandidateId, buildBoard, clip, laneBands, mergeRuns, operationIndex,
  operationState, orderProjections, planSpans, topologicalRoute,
} from './scheduleModel'
import type { Publication } from './contracts'
import { actual, assignment, boardSnapshot, day, shiftCalendar, zone } from './test/boardFixture'

const bounds = dayBounds(day, zone)
const at = (iso: string) => Date.parse(iso)

describe('operation ID expansion', () => {
  it('mirrors the backend batch and operation ID rules', () => {
    const index = operationIndex(boardSnapshot())
    expect([...index.keys()]).toEqual([
      'SO-001-R001-B001-OP10', 'SO-001-R001-B001-OP20',
      'SO-001-R001-B002-OP10', 'SO-001-R001-B002-OP20',
    ])
    const first = index.get('SO-001-R001-B001-OP20')
    expect(first).toMatchObject({
      orderId: 'SO-001', batchId: 'SO-001-R001-B001', batchIndex: 1,
      productName: 'Deep groove ball bearing 6202', operationCode: 'OP20', stepName: 'Ring assembly', quantity: 50, sequence: 2,
    })
  })

  it('does not expand an order whose quantity cannot be split into whole batches', () => {
    const snapshot = boardSnapshot()
    snapshot.orders = [{ ...snapshot.orders[0]!, quantity: 51 }]
    expect(operationIndex(snapshot).size).toBe(0)
  })

  it('persistent batches allow gaps in numbering, and cancelled batches are not regenerated from the new order quantity', () => {
    const snapshot = boardSnapshot()
    snapshot.production_batches = [
      { batch_id: 'SO-001-R001-B001', order_id: 'SO-001', product_id: 'BRG-6202', route_version: 'route-1', quantity: 50, sequence: 1, purpose: 'CANCELLED' },
      { batch_id: 'SO-001-R001-B007', order_id: 'SO-001', product_id: 'BRG-6202', route_version: 'route-1', quantity: 50, sequence: 7, purpose: 'CUSTOMER' },
      { batch_id: 'SO-001-R001-B009', order_id: 'SO-001', product_id: 'BRG-6202', route_version: 'route-1', quantity: 50, sequence: 9, purpose: 'CUSTOMER' },
    ]
    const index = operationIndex(snapshot)
    expect([...index.keys()]).toEqual(['SO-001-R001-B007-OP10', 'SO-001-R001-B007-OP20', 'SO-001-R001-B009-OP10', 'SO-001-R001-B009-OP20'])
    expect(index.get('SO-001-R001-B009-OP20')).toMatchObject({ batchIndex: 9, quantity: 50, routeVersion: 'route-1' })
  })

  it('WIP moved to stock after an order cancellation keeps real operation names, quantities and order grouping', () => {
    const operationId = 'SO-001-R001-B007-OP10', snapshot = boardSnapshot()
    snapshot.orders[0] = { ...snapshot.orders[0]!, quantity: 0, status: 'CANCELLED' }
    snapshot.production_batches = [{ batch_id: 'SO-001-R001-B007', order_id: 'SO-001', product_id: 'BRG-6202', route_version: 'route-1', quantity: 50, sequence: 7, purpose: 'STOCK' }]
    snapshot.actuals = [actual({ operation_id: operationId, batch_id: 'SO-001-R001-B007' })]
    const board = buildBoard({ snapshot, assignments: [assignment({ operation_id: operationId })], dayStartMs: bounds.startMs, dayEndMs: bounds.endMs, grouping: 'order' })
    expect(board.rows[0]?.identity).toMatchObject({ orderId: 'SO-001', batchId: 'SO-001-R001-B007', stepName: 'Kitting', quantity: 50 })
    expect(board.lanes[0]?.id).toBe('SO-001')
    expect(board.rows[0]?.actual?.batch_id).toBe('SO-001-R001-B007')
  })

  it('an empty persistent batch set does not fall back to rebuilding order batches; null stays compatible with old snapshots', () => {
    expect(operationIndex(boardSnapshot({ production_batches: [] })).size).toBe(0)
    expect(operationIndex(boardSnapshot({ production_batches: null })).size).toBe(4)
  })

  it('sorts by route dependencies instead of operation codes', () => {
    const steps = [
      { step_id: 'C', product_id: 'P', operation_code: 'OP30', name: 'Three', skill: 'S', predecessors: ['B'] },
      { step_id: 'A', product_id: 'P', operation_code: 'OP10', name: 'One', skill: 'S', predecessors: [] },
      { step_id: 'B', product_id: 'P', operation_code: 'OP60', name: 'Two', skill: 'S', predecessors: ['A'] },
    ]
    expect(topologicalRoute(steps).map((step) => step.step_id)).toEqual(['A', 'B', 'C'])
  })

  it('keeps the remaining operations instead of dropping them when dependencies cannot be confirmed', () => {
    const steps = [
      { step_id: 'A', product_id: 'P', operation_code: 'OP10', name: 'One', skill: 'S', predecessors: ['B'] },
      { step_id: 'B', product_id: 'P', operation_code: 'OP20', name: 'Two', skill: 'S', predecessors: ['A'] },
    ]
    expect(topologicalRoute(steps)).toHaveLength(2)
  })
})

describe('time semantics of plan and actual', () => {
  it('changeover runs from changeover_start to start_at and production from start_at to end_at', () => {
    expect(planSpans(assignment({ operation_id: 'x' }))).toEqual([
      { phase: 'setup', startMs: at(`${day}T00:30:00Z`), endMs: at(`${day}T00:35:00Z`) },
      { phase: 'production', startMs: at(`${day}T00:35:00Z`), endMs: at(`${day}T01:00:00Z`) },
    ])
  })

  it('draws no changeover segment for a zero-minute changeover', () => {
    const spans = planSpans(assignment({ operation_id: 'x', changeover_start: `${day}T00:35:00Z` }))
    expect(spans).toHaveLength(1)
    expect(spans[0]!.phase).toBe('production')
  })

  it('skips execution history and the interruption gap when resuming and draws only the remaining work', () => {
    const spans = planSpans(assignment({
      operation_id: 'x',
      changeover_start: `${day}T00:30:00Z`,
      start_at: `${day}T00:35:00Z`,
      resume_changeover_start: `${day}T02:00:00Z`,
      resume_at: `${day}T02:05:00Z`,
      end_at: `${day}T02:20:00Z`,
    }))
    expect(spans).toEqual([
      { phase: 'setup', startMs: at(`${day}T00:30:00Z`), endMs: at(`${day}T00:35:00Z`) },
      { phase: 'setup', startMs: at(`${day}T02:00:00Z`), endMs: at(`${day}T02:05:00Z`) },
      { phase: 'production', startMs: at(`${day}T02:05:00Z`), endMs: at(`${day}T02:20:00Z`) },
    ])
  })

  it('draws actual execution from segment records with gaps for interruptions', () => {
    const result = actualSpans(actual({
      operation_id: 'x',
      segments: [
        { phase: 'SETUP', start_at: `${day}T00:30:00Z`, end_at: `${day}T00:35:00Z`, source_event_id: 'e1' },
        { phase: 'PRODUCTION', start_at: `${day}T00:35:00Z`, end_at: `${day}T00:40:00Z`, source_event_id: 'e2' },
        { phase: 'PRODUCTION', start_at: `${day}T00:50:00Z`, end_at: `${day}T00:55:00Z`, source_event_id: 'e3' },
      ],
    }), at(`${day}T01:12:00Z`))
    expect(result.approximate).toBe(false)
    expect(result.spans).toHaveLength(3)
    expect(result.spans[1]!.endMs).toBeLessThan(result.spans[2]!.startMs)
  })

  it('gives only a rough interval marked approximate when segment records are missing', () => {
    const record = actual({ operation_id: 'x' })
    delete record.segments
    const result = actualSpans(record, at(`${day}T01:12:00Z`))
    expect(result.approximate).toBe(true)
    expect(result.spans).toEqual([{ phase: 'production', startMs: at(`${day}T00:30:00Z`), endMs: at(`${day}T01:12:00Z`) }])
  })

  it('clips intervals that cross days at the day boundary', () => {
    const spans = clip([{ phase: 'production', startMs: bounds.startMs - 3_600_000, endMs: bounds.endMs + 3_600_000 }], bounds.startMs, bounds.endMs)
    expect(spans).toEqual([{ phase: 'production', startMs: bounds.startMs, endMs: bounds.endMs }])
  })
})

describe('operation display state', () => {
  const now = at(`${day}T01:12:00Z`)
  it('interrupted with unconfirmed remaining work is its own state', () => {
    expect(operationState(actual({ operation_id: 'x', state: 'BLOCKED', remaining_minutes: null }), null, now)).toBe('UNCONFIRMED')
    expect(operationState(actual({ operation_id: 'x', state: 'BLOCKED', remaining_minutes: 12 }), null, now)).toBe('BLOCKED')
  })

  it('completed operations are split into passed and awaiting QC by the quality result', () => {
    const base = { operation_id: 'x', state: 'COMPLETED' as const, actual_end: `${day}T01:00:00Z`, completed_quantity: 50 }
    expect(operationState(actual({ ...base, quality_state: 'PASSED' }), null, now)).toBe('DONE')
    expect(operationState(actual({ ...base, quality_state: 'UNKNOWN' }), null, now)).toBe('UNJUDGED')
    expect(operationState(actual({ ...base, quality_state: 'FAILED' }), null, now)).toBe('BLOCKED')
  })

  it('shows changeover and production separately', () => {
    expect(operationState(actual({ operation_id: 'x', state: 'SETUP', actual_start: null }), null, now)).toBe('SETUP')
    expect(operationState(actual({ operation_id: 'x', state: 'IN_PROGRESS' }), null, now)).toBe('RUNNING')
  })

  it('flags rescheduling when the planned slot has passed without an execution record', () => {
    const past = assignment({ operation_id: 'x', end_at: `${day}T01:00:00Z` })
    expect(operationState(null, past, now)).toBe('REPLAN')
    const future = assignment({ operation_id: 'x', start_at: `${day}T02:00:00Z`, end_at: `${day}T02:30:00Z` })
    expect(operationState(null, future, now)).toBe('PLANNED')
  })
})

describe('lane shift shading', () => {
  it('gaps between calendar windows show as non-working time and the overtime window is marked separately', () => {
    const bands = laneBands(shiftCalendar(), [], bounds.startMs, bounds.endMs, null)
    expect(bands[0]).toMatchObject({ kind: 'CLOSED' })
    expect(bands.filter((band) => band.kind === 'NORMAL')).toHaveLength(2)
    expect(bands.filter((band) => band.kind === 'OVERTIME')).toHaveLength(1)
  })

  it('returns nothing without a calendar so the interface says shifts are not provided instead of assuming hours', () => {
    expect(laneBands(undefined, [], bounds.startMs, bounds.endMs, null)).toEqual([])
  })

  it('a down machine masks the rest of the time from the current factory time and gives the reason', () => {
    const bands = laneBands(shiftCalendar(), [], bounds.startMs, bounds.endMs, { fromMs: at(`${day}T01:12:00Z`), reason: 'Down; recovery time not confirmed' })
    const mask = bands.find((band) => band.kind === 'UNAVAILABLE')
    expect(mask).toMatchObject({ startMs: at(`${day}T01:12:00Z`), endMs: bounds.endMs, reason: 'Down; recovery time not confirmed' })
  })
})

describe('board model', () => {
  it('groups by machine and keeps machine rows without work', () => {
    const snapshot = boardSnapshot()
    const board = buildBoard({
      snapshot,
      assignments: [
        assignment({ operation_id: 'SO-001-R001-B001-OP10', changeover_start: `${day}T02:00:00Z`, start_at: `${day}T02:05:00Z`, end_at: `${day}T02:30:00Z` }),
        assignment({ operation_id: 'SO-001-R001-B002-OP10' }),
      ],
      dayStartMs: bounds.startMs,
      dayEndMs: bounds.endMs,
      grouping: 'resource',
    })
    expect(board.lanes.map((lane) => lane.id)).toEqual(['KIT-01', 'ASM-01'])
    expect(board.lanes[0]!.rows).toHaveLength(2)
    expect(board.lanes[1]!.rows).toHaveLength(0)
    expect(board.lanes[1]!.statusLabel).toBe('Down')
    // Future work is planned; past work without an execution record is flagged for rescheduling.
    expect(board.summary.byState.PLANNED).toBe(1)
    expect(board.summary.byState.REPLAN).toBe(1)
    expect(board.summary.orders).toBe(1)
  })

  it('operations with only an actual record and no current plan are still shown', () => {
    const snapshot = boardSnapshot({ actuals: [actual({ operation_id: 'SO-001-R001-B002-OP10' })] })
    const board = buildBoard({ snapshot, assignments: [], dayStartMs: bounds.startMs, dayEndMs: bounds.endMs, grouping: 'resource' })
    expect(board.rows.map((row) => row.operationId)).toEqual(['SO-001-R001-B002-OP10'])
    expect(board.rows[0]!.state).toBe('RUNNING')
  })

  it('work outside the day does not appear on this screen', () => {
    const board = buildBoard({
      snapshot: boardSnapshot(),
      assignments: [assignment({ operation_id: 'SO-001-R001-B001-OP10', changeover_start: '2026-09-16T00:30:00Z', start_at: '2026-09-16T00:35:00Z', end_at: '2026-09-16T01:00:00Z' })],
      dayStartMs: bounds.startMs,
      dayEndMs: bounds.endMs,
      grouping: 'resource',
    })
    expect(board.rows).toHaveLength(0)
  })

  it('counts planned minutes inside the overtime window', () => {
    const board = buildBoard({
      snapshot: boardSnapshot(),
      assignments: [assignment({ operation_id: 'SO-001-R001-B001-OP10', changeover_start: `${day}T09:30:00Z`, start_at: `${day}T09:30:00Z`, end_at: `${day}T10:30:00Z` })],
      dayStartMs: bounds.startMs,
      dayEndMs: bounds.endMs,
      grouping: 'resource',
    })
    expect(board.summary.overtimeMinutes).toBe(60)
  })

  it('marks changed operations when compared with the active plan', () => {
    const snapshot = boardSnapshot()
    const baseline = [assignment({ operation_id: 'SO-001-R001-B001-OP10' })]
    const board = buildBoard({
      snapshot,
      assignments: [assignment({ operation_id: 'SO-001-R001-B001-OP10', start_at: `${day}T00:40:00Z`, end_at: `${day}T01:05:00Z` })],
      baseline,
      dayStartMs: bounds.startMs,
      dayEndMs: bounds.endMs,
      grouping: 'resource',
    })
    expect(board.rows[0]!.changed).toBe(true)
  })

  it('grouping by order puts the operations of one order in one row', () => {
    const board = buildBoard({
      snapshot: boardSnapshot(),
      assignments: [
        assignment({ operation_id: 'SO-001-R001-B001-OP10' }),
        assignment({ operation_id: 'SO-001-R001-B002-OP10', changeover_start: `${day}T01:00:00Z`, start_at: `${day}T01:05:00Z`, end_at: `${day}T01:30:00Z` }),
      ],
      dayStartMs: bounds.startMs,
      dayEndMs: bounds.endMs,
      grouping: 'order',
    })
    expect(board.lanes).toHaveLength(1)
    expect(board.lanes[0]!.id).toBe('SO-001')
    expect(board.lanes[0]!.rows).toHaveLength(2)
  })
})

describe('merging consecutive similar operations', () => {
  const rowsOf = (assignments: ReturnType<typeof assignment>[], snapshot = boardSnapshot()) =>
    buildBoard({ snapshot, assignments, dayStartMs: bounds.startMs, dayEndMs: bounds.endMs, grouping: 'resource' })
      .lanes.find((lane) => lane.id === 'KIT-01')!.rows

  it('merges consecutive batches of the same order and operation into one bar with the batch count', () => {
    const rows = rowsOf([
      assignment({ operation_id: 'SO-001-R001-B001-OP10', changeover_start: `${day}T02:00:00Z`, start_at: `${day}T02:00:00Z`, end_at: `${day}T02:10:00Z` }),
      assignment({ operation_id: 'SO-001-R001-B002-OP10', changeover_start: `${day}T02:10:00Z`, start_at: `${day}T02:10:00Z`, end_at: `${day}T02:20:00Z` }),
    ])
    const runs = mergeRuns(rows)
    expect(runs).toHaveLength(1)
    expect(runs[0]).toMatchObject({
      orderId: 'SO-001', operationCode: 'OP10', count: 2, state: 'PLANNED',
      startMs: at(`${day}T02:00:00Z`), endMs: at(`${day}T02:20:00Z`),
    })
    expect(runs[0]!.operationIds).toEqual(['SO-001-R001-B001-OP10', 'SO-001-R001-B002-OP10'])
  })

  it('different operations do not merge even when adjacent in time', () => {
    const rows = rowsOf([
      assignment({ operation_id: 'SO-001-R001-B001-OP10', changeover_start: `${day}T02:00:00Z`, start_at: `${day}T02:00:00Z`, end_at: `${day}T02:10:00Z` }),
      assignment({ operation_id: 'SO-001-R001-B001-OP20', changeover_start: `${day}T02:10:00Z`, start_at: `${day}T02:10:00Z`, end_at: `${day}T02:20:00Z` }),
    ])
    expect(mergeRuns(rows).map((run) => run.operationCode)).toEqual(['OP10', 'OP20'])
  })

  it('different states do not merge, so problems are not absorbed by normal operations', () => {
    const snapshot = boardSnapshot({
      actuals: [actual({ operation_id: 'SO-001-R001-B002-OP10', state: 'BLOCKED', remaining_minutes: null, remaining_setup_minutes: null })],
    })
    const rows = rowsOf([
      assignment({ operation_id: 'SO-001-R001-B001-OP10', changeover_start: `${day}T02:00:00Z`, start_at: `${day}T02:00:00Z`, end_at: `${day}T02:10:00Z` }),
      assignment({ operation_id: 'SO-001-R001-B002-OP10', changeover_start: `${day}T02:10:00Z`, start_at: `${day}T02:10:00Z`, end_at: `${day}T02:20:00Z` }),
    ], snapshot)
    expect(mergeRuns(rows).map((run) => run.state).sort()).toEqual(['PLANNED', 'UNCONFIRMED'])
  })

  it('clearly separated times do not merge', () => {
    const rows = rowsOf([
      assignment({ operation_id: 'SO-001-R001-B001-OP10', changeover_start: `${day}T02:00:00Z`, start_at: `${day}T02:00:00Z`, end_at: `${day}T02:10:00Z` }),
      assignment({ operation_id: 'SO-001-R001-B002-OP10', changeover_start: `${day}T04:00:00Z`, start_at: `${day}T04:00:00Z`, end_at: `${day}T04:10:00Z` }),
    ])
    expect(mergeRuns(rows)).toHaveLength(2)
  })

  it('operations with an unidentified order form their own bars and merge with nobody', () => {
    const rows = rowsOf([
      assignment({ operation_id: 'UNKNOWN-A', changeover_start: `${day}T02:00:00Z`, start_at: `${day}T02:00:00Z`, end_at: `${day}T02:10:00Z` }),
      assignment({ operation_id: 'UNKNOWN-B', changeover_start: `${day}T02:10:00Z`, start_at: `${day}T02:10:00Z`, end_at: `${day}T02:20:00Z` }),
    ])
    expect(mergeRuns(rows)).toHaveLength(2)
  })

  it('the total batch count after merging equals the original number of operations', () => {
    const rows = rowsOf([
      assignment({ operation_id: 'SO-001-R001-B001-OP10', changeover_start: `${day}T02:00:00Z`, start_at: `${day}T02:00:00Z`, end_at: `${day}T02:10:00Z` }),
      assignment({ operation_id: 'SO-001-R001-B002-OP10', changeover_start: `${day}T02:10:00Z`, start_at: `${day}T02:10:00Z`, end_at: `${day}T02:20:00Z` }),
      assignment({ operation_id: 'SO-001-R001-B001-OP20', changeover_start: `${day}T02:20:00Z`, start_at: `${day}T02:20:00Z`, end_at: `${day}T02:30:00Z` }),
    ])
    expect(mergeRuns(rows).reduce((total, run) => total + run.count, 0)).toBe(rows.length)
  })
})

describe('order due dates and the active plan', () => {
  it('gives no planned completion when operations are not fully scheduled', () => {
    const snapshot = boardSnapshot()
    const index = operationIndex(snapshot)
    const partial = orderProjections(snapshot.orders, [assignment({ operation_id: 'SO-001-R001-B001-OP10' })], index)
    expect(partial[0]).toMatchObject({ covered: false, late: false })
    expect(Number.isNaN(partial[0]!.plannedEndMs)).toBe(true)
  })

  it('a fully scheduled completion after the due date counts as late', () => {
    const snapshot = boardSnapshot()
    const index = operationIndex(snapshot)
    const all = [...index.keys()].map((id) => assignment({ operation_id: id, end_at: `${day}T10:00:00Z` }))
    expect(orderProjections(snapshot.orders, all, index)[0]).toMatchObject({ covered: true, late: true })
  })

  it('only a release the execution source confirmed as effective counts as the active plan', () => {
    const release = (state: Publication['release']['source_state'], candidate: string, committed: string): Publication => ({
      candidate_id: candidate,
      error_code: null,
      release: {
        release_id: `r-${candidate}`, operation_id: `o-${candidate}`, factory_id: 'skf-workshop',
        candidate_hash: 'h', payload_hash: 'p', approval_ids: ['a'], local_state: 'LOCAL_COMMITTED',
        source_state: state, execution_state: 'NOT_STARTED', source_receipt_id: state === 'ACTIVE' ? 'receipt' : null,
        committed_at: committed, effective_at: state === 'ACTIVE' ? committed : null,
      },
    })
    expect(activeCandidateId([release('PENDING_SOURCE', 'c1', `${day}T00:00:00Z`)])).toBe('')
    expect(activeCandidateId([
      release('ACTIVE', 'c1', `${day}T00:00:00Z`),
      release('ACTIVE', 'c2', `${day}T01:00:00Z`),
    ])).toBe('c2')
    expect(activeCandidateId(undefined)).toBe('')
  })
})
