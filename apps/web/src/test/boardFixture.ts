import type { ActualExecution, Assignment, CalendarWindow, Snapshot } from '../contracts'

/** Factory time zone Asia/Singapore (UTC+8). The calendar matches the seed:
 *  local 08:30–12:00 regular shift, 12:00–13:00 break (a gap between windows), 13:00–17:30 regular shift, 17:30–19:30 overtime window. */
export const zone = 'Asia/Singapore'
export const day = '2026-09-14'

export const shiftCalendar = (date = day): CalendarWindow[] => [
  { start_at: `${date}T00:30:00Z`, end_at: `${date}T04:00:00Z`, kind: 'NORMAL' },
  { start_at: `${date}T05:00:00Z`, end_at: `${date}T09:30:00Z`, kind: 'NORMAL' },
  { start_at: `${date}T09:30:00Z`, end_at: `${date}T11:30:00Z`, kind: 'OVERTIME' },
]

export function boardSnapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    schema_version: 'byof.snapshot/2',
    snapshot_id: 'snap-1',
    factory_id: 'skf-workshop',
    run_id: 'run-1',
    snapshot_clock: `${day}T01:12:00Z`,
    horizon: { start_at: `${day}T00:30:00Z`, end_at: '2026-09-16T09:30:00Z' },
    active_plan_version: null,
    content_hash: 'hash-snapshot',
    source: {
      source_system: 'factory-simulator',
      observed_at: `${day}T01:10:00Z`,
      effective_at: `${day}T01:10:00Z`,
      ownership: 'simulator_fact',
      freshness: 'CURRENT',
      complete: true,
      source_revision: 'rev-1',
    },
    profile: {
      timezone: zone,
      version: 'profile-1',
      policy: { policy_version: 'policy-1', progress_revalidation_enabled: true, freeze_window_min: 60 },
      products: [{ product_id: 'BRG-6202', name: 'Deep groove ball bearing 6202', batch_size: 50, route_version: 'route-1' }],
      materials: [{ material_id: 'MAT-RING', name: 'Ring', unit: 'EA' }],
      routes: [
        { step_id: 'S10', product_id: 'BRG-6202', operation_code: 'OP10', name: 'Kitting', skill: 'KIT', predecessors: [], resource_type: 'KITTING', setup_min: 5, cycle_sec_per_unit: 30 },
        { step_id: 'S20', product_id: 'BRG-6202', operation_code: 'OP20', name: 'Ring assembly', skill: 'ASM', predecessors: ['S10'], resource_type: 'ASSEMBLY_CELL', setup_min: 5, cycle_sec_per_unit: 60 },
      ],
    },
    orders: [{ order_id: 'SO-001', product_id: 'BRG-6202', quantity: 100, due_at: `${day}T09:30:00Z`, hard_deadline: false, status: 'IN_PROGRESS', split_revision: 1, priority_weight: 1, version: 1 }],
    inventory: [{ material_id: 'MAT-RING', unit: 'EA', on_hand: 500, reserved: 100, version: 1 }],
    receipts: [],
    resources: [
      { resource_id: 'KIT-01', resource_type: 'KITTING', operation_codes: ['OP10'], status: 'AVAILABLE', calendar: shiftCalendar(), unavailable: [] },
      { resource_id: 'ASM-01', resource_type: 'ASSEMBLY_CELL', operation_codes: ['OP20'], status: 'DOWN', calendar: shiftCalendar(), unavailable: [] },
    ],
    workers: [
      { worker_id: 'W01', skills: ['KIT'], status: 'AVAILABLE', overtime_available: true, calendar: shiftCalendar(), unavailable: [] },
      { worker_id: 'W02', skills: ['ASM'], status: 'AVAILABLE', overtime_available: false, calendar: shiftCalendar(), unavailable: [] },
    ],
    actuals: [],
    reservations: [],
    ...overrides,
  }
}

export function assignment(overrides: Partial<Assignment> & { operation_id: string }): Assignment {
  return {
    resource_id: 'KIT-01',
    worker_id: 'W01',
    changeover_start: `${day}T00:30:00Z`,
    start_at: `${day}T00:35:00Z`,
    end_at: `${day}T01:00:00Z`,
    ...overrides,
  }
}

export function actual(overrides: Partial<ActualExecution> & { operation_id: string }): ActualExecution {
  return {
    state: 'IN_PROGRESS',
    completed_quantity: 0,
    actual_start: `${day}T00:35:00Z`,
    actual_end: null,
    quality_state: 'PENDING',
    changeover_start: `${day}T00:30:00Z`,
    batch_id: 'SO-001-R001-B001',
    resource_id: 'KIT-01',
    worker_id: 'W01',
    remaining_minutes: 10,
    remaining_setup_minutes: 0,
    segments: [
      { phase: 'SETUP', start_at: `${day}T00:30:00Z`, end_at: `${day}T00:35:00Z`, source_event_id: 'e1' },
      { phase: 'PRODUCTION', start_at: `${day}T00:35:00Z`, end_at: `${day}T00:50:00Z`, source_event_id: 'e2' },
    ],
    ...overrides,
  }
}
