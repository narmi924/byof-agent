import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { BoardView } from './BoardView'
import type { Assignment, Candidate, CandidateRecord, Snapshot, Workspace } from './contracts'
import { readViewState } from './viewState'
import type { ViewState } from './viewState'
import { actual, assignment, boardSnapshot, day } from './test/boardFixture'
import { baselineEffective } from './test/preferenceFixture'
import { declaredExecutionSupport } from './test/executionFixture'

function candidate(assignments: Assignment[], overrides: Partial<Candidate> = {}): CandidateRecord {
  return {
    snapshot_id: 'snap-1',
    state: 'CANDIDATE',
    approvals: [],
    candidate: {
      candidate_id: 'cand-1', factory_id: 'skf-workshop', version: 1, content_hash: 'hash-candidate',
      binding: { snapshot_hash: 'hash-snapshot', profile_version: 'profile-1', policy_version: 'policy-1', objective_version: 'delivery-v1', baseline_plan_version: null },
      native_status: 'FEASIBLE', has_solution: true, termination_reason: 'COMPLETED',
      objective: [], proven_objective_levels: 0, assignments, scenario: [], required_consents: [],
      checker: { status: 'PASS', issues: [] },
      effective_not_before: `${day}T01:00:00Z`, accept_before: `${day}T09:00:00Z`,
      ...overrides,
    },
  }
}

function workspace(snapshot: Snapshot | null, candidates: CandidateRecord[] = []): Workspace {
  return {
    snapshot,
    last_synced_at: `${day}T01:12:00Z`,
    freshness: 'CURRENT',
    jobs: [],
    candidates,
    publications: [],
    objective_state: baselineEffective,
    execution_support: declaredExecutionSupport(),
  }
}

function view(value: Workspace | null, patch: Partial<ViewState> = {}) {
  const navigate = vi.fn()
  const state: ViewState = { ...readViewState(''), factoryId: 'skf-workshop', ...patch }
  render(<BoardView
    workspace={value}
    state={state}
    navigate={navigate}
    tasks={[]}
  />)
  return { navigate }
}

const plan = [
  assignment({ operation_id: 'SO-001-R001-B001-OP10' }),
  assignment({ operation_id: 'SO-001-R001-B001-OP20', resource_id: 'ASM-01', worker_id: 'W02', changeover_start: `${day}T02:00:00Z`, start_at: `${day}T02:05:00Z`, end_at: `${day}T03:00:00Z` }),
]

describe('daily schedule board', () => {
  it.each(['STOCK', 'CUSTOMER'] as const)('with batch ledger %s the operation detail shows the real production purpose', purpose => {
    const snapshot = boardSnapshot()
    snapshot.production_batches = [{ batch_id: 'SO-001-R001-B001', order_id: 'SO-001', product_id: 'BRG-6202', route_version: 'route-1', quantity: 50, sequence: 1, purpose }]
    if (purpose === 'STOCK') snapshot.orders = [{ ...snapshot.orders[0]!, quantity: 0, status: 'CANCELLED' }]
    view(workspace(snapshot, [candidate(plan)]), { operationId: 'SO-001-R001-B001-OP10' })
    const drawer = screen.getByRole('dialog', { name: /^Operation / })
    expect(within(drawer).getByText(purpose === 'STOCK' ? 'To stock' : 'Customer delivery')).toBeVisible()
    if (purpose === 'STOCK') expect(within(drawer).getByText(/Once all operations are done and it passes quality checks it counts as finished stock/)).toBeVisible()
    else expect(within(drawer).queryByText(/not customer delivery/)).not.toBeInTheDocument()
  })

  it('shows one day per screen with the date, weekday and current factory time in the title', () => {
    view(workspace(boardSnapshot(), [candidate(plan)]))
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Sep 14 Mon')
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Today')
    expect(screen.getByText(/Factory time 09:12/)).toBeVisible()
    expect(screen.getByText(/UTC\+8/)).toBeVisible()
  })

  it('lists lanes by machine and gives down machines an explicit status', () => {
    view(workspace(boardSnapshot(), [candidate(plan)]))
    expect(screen.getByText('KIT-01')).toBeVisible()
    expect(screen.getByText('ASM-01')).toBeVisible()
    expect(screen.getByText('Down')).toBeVisible()
  })

  it('after switching runs the old run plans are not shown as the current schedule', () => {
    const old = candidate(plan)
    old.run_id = 'previous-run'
    view(workspace(boardSnapshot(), [old]))
    expect(screen.queryByRole('combobox', { name: 'Plan shown' })).not.toBeInTheDocument()
    expect(screen.getByText(/No production scheduled/)).toBeVisible()
  })

  it('each operation block carries batch, operation and state text, not only color', () => {
    view(workspace(boardSnapshot(), [candidate(plan)]))
    // Work after 09:12 is planned; earlier work without an execution record is flagged for rescheduling.
    expect(screen.getByRole('button', { name: 'B001·OP20 Planned' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'B001·OP10 Needs rescheduling' })).toBeVisible()
  })

  it('actual execution records decide the state; interrupted with unconfirmed remaining work is its own kind', () => {
    const snapshot = boardSnapshot({
      actuals: [actual({ operation_id: 'SO-001-R001-B001-OP20', state: 'BLOCKED', remaining_minutes: null, remaining_setup_minutes: null, resource_id: 'ASM-01', worker_id: 'W02' })],
    })
    view(workspace(snapshot, [candidate(plan)]))
    expect(screen.getByRole('button', { name: 'B001·OP20 Remaining work unconfirmed' })).toBeVisible()
  })

  it('the state summary counts the day and problems lead straight back to the Agent conversation', async () => {
    const snapshot = boardSnapshot({
      actuals: [actual({ operation_id: 'SO-001-R001-B001-OP20', state: 'BLOCKED', remaining_minutes: null, remaining_setup_minutes: null, resource_id: 'ASM-01', worker_id: 'W02' })],
    })
    const { navigate } = view(workspace(snapshot, [candidate(plan)]))
    await userEvent.click(screen.getByRole('button', { name: /Interrupted/ }))
    expect(navigate).toHaveBeenCalledWith({ view: 'chat' }, true)
  })

  it('the day switcher at the top only changes the shown day, not factory data', async () => {
    const { navigate } = view(workspace(boardSnapshot(), [candidate(plan)]))
    const strip = screen.getByRole('group', { name: 'Days in the scheduling window' })
    await userEvent.click(within(strip).getByRole('button', { name: /Sep 15/ }))
    expect(navigate).toHaveBeenCalledWith({ day: '2026-09-15', operationId: '' })
    expect(within(strip).getAllByRole('button')[0]).toHaveAttribute('aria-current', 'date')
  })

  it('every day in the scheduling window can be reached directly', async () => {
    const { navigate } = view(workspace(boardSnapshot(), [candidate(plan)]))
    const strip = screen.getByRole('group', { name: 'Days in the scheduling window' })
    expect(within(strip).getAllByRole('button')).toHaveLength(3)
    await userEvent.click(within(strip).getByRole('button', { name: /Sep 16/ }))
    expect(navigate).toHaveBeenCalledWith({ day: '2026-09-16', operationId: '' })
  })

  it('opening an operation shows plan versus actual, remaining work and actual segments', async () => {
    const snapshot = boardSnapshot({ actuals: [actual({ operation_id: 'SO-001-R001-B001-OP10' })] })
    view(workspace(snapshot, [candidate(plan)]), { operationId: 'SO-001-R001-B001-OP10' })
    const drawer = screen.getByRole('dialog', { name: /^Operation / })
    expect(within(drawer).getByRole('heading', { name: 'OP10 Kitting' })).toBeVisible()
    expect(within(drawer).getByText('SO-001 · Deep groove ball bearing 6202')).toBeVisible()
    expect(within(drawer).getByText('SO-001-R001-B001 · 50 pcs per batch')).toBeVisible()
    expect(within(drawer).getByText('KIT-01 · W01')).toBeVisible()
    expect(within(drawer).getByText('08:30 → 09:00')).toBeVisible()
    expect(within(drawer).getByText('10 min / 0 min')).toBeVisible()
    expect(within(drawer).getByText(/Changeover 08:30 → 08:35/)).toBeVisible()
    expect(within(drawer).getByText(/Production 08:35 → 08:50/)).toBeVisible()
  })

  it('without segment records it says the interval is approximate and invents no segments', () => {
    const record = actual({ operation_id: 'SO-001-R001-B001-OP10' })
    delete record.segments
    view(workspace(boardSnapshot({ actuals: [record] }), [candidate(plan)]), { operationId: 'SO-001-R001-B001-OP10' })
    const drawer = screen.getByRole('dialog', { name: /^Operation / })
    expect(within(drawer).getByText('Actual segments not provided')).toBeVisible()
    expect(within(drawer).getByText('The source has not provided segment records; the actual interval above is approximate.')).toBeVisible()
  })

  it('interrupted with unconfirmed remaining work says the shop floor must confirm first', () => {
    const record = actual({ operation_id: 'SO-001-R001-B001-OP10', state: 'BLOCKED', remaining_minutes: null, remaining_setup_minutes: null })
    view(workspace(boardSnapshot({ actuals: [record] }), [candidate(plan)]), { operationId: 'SO-001-R001-B001-OP10' })
    expect(screen.getByText('The remaining work has no confirmed source. Before rescheduling this operation, the shop floor must confirm the recovery time and remaining minutes.')).toBeVisible()
  })

  it('says so explicitly when the shift calendar is missing and assumes no hours', () => {
    const snapshot = boardSnapshot()
    snapshot.resources = snapshot.resources.map((item) => { const copy = { ...item }; delete copy.calendar; return copy })
    view(workspace(snapshot, [candidate(plan)]))
    expect(screen.getByText(/have no shift calendar from the source/)).toBeVisible()
  })

  it('without scheduled work it explains the situation and gives a next step instead of an empty board', () => {
    view(workspace(boardSnapshot()))
    expect(screen.getByText(/No production scheduled/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Open the Agent conversation' })).toBeVisible()
  })

  it('draws no board before facts are synced and says they are read automatically', () => {
    view(workspace(null))
    expect(screen.getByRole('heading', { name: 'Factory facts not synced yet' })).toBeVisible()
    expect(screen.getByText(/it appears automatically once connected/)).toBeVisible()
    expect(screen.queryByRole('group', { name: /^Schedule for/ })).not.toBeInTheDocument()
  })

  it('names preview plans after their conversation and can go straight back to it', async () => {
    const preview = { ...candidate(plan), case_id: 'case-9' }
    const onOpenConversation = vi.fn()
    const state: ViewState = { ...readViewState(''), factoryId: 'skf-workshop', candidateId: 'cand-1' }
    render(<BoardView workspace={workspace(boardSnapshot(), [preview])} state={state} navigate={vi.fn()} tasks={[]}
      conversations={[{ case_id: 'case-9', title: 'Check against the latest factory facts: order SO-003 demand changed from 1200 to 1500 pcs' }]} onOpenConversation={onOpenConversation} />)
    expect(screen.getByDisplayValue(/^order SO-003 demand changed from 1200/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Back to its conversation' }))
    expect(onOpenConversation).toHaveBeenCalledWith('case-9', 'cand-1')
  })

  it('marks operations that differ from the active plan when previewing a plan', () => {
    const effective = candidate(plan)
    effective.candidate.candidate_id = 'cand-effective'
    const changed = candidate([
      assignment({ operation_id: 'SO-001-R001-B001-OP20', resource_id: 'ASM-01', worker_id: 'W02', changeover_start: `${day}T04:00:00Z`, start_at: `${day}T04:05:00Z`, end_at: `${day}T05:00:00Z` }),
    ])
    const value = workspace(boardSnapshot(), [effective, changed])
    value.publications = [{
      candidate_id: 'cand-effective',
      error_code: null,
      release: {
        release_id: 'r1', operation_id: 'o1', factory_id: 'skf-workshop', candidate_hash: 'hash-candidate',
        payload_hash: 'p', approval_ids: ['a'], local_state: 'LOCAL_COMMITTED', source_state: 'ACTIVE',
        execution_state: 'NOT_STARTED', source_receipt_id: 'receipt', committed_at: `${day}T00:00:00Z`, effective_at: `${day}T00:00:00Z`,
      },
    }]
    view(value, { candidateId: 'cand-1' })
    expect(screen.getByText(/Preview/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'B001·OP20 Planned' }).className).toContain('op-hit')
  })

  it('grouping by order puts the operations of one order in one row', async () => {
    const { navigate } = view(workspace(boardSnapshot(), [candidate(plan)]))
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Group by' }), 'order')
    expect(navigate).toHaveBeenCalledWith({ group: 'order' })
  })

  it('dense operations merge by default, and the block says which order and operation it is and how many batches', async () => {
    // One machine runs 30 batches of the same order and operation in a row: drawn per batch each block is a few pixels.
    const snapshot = boardSnapshot()
    snapshot.orders = [{ ...snapshot.orders[0]!, quantity: 1500 }]
    const dense = [...Array(30).keys()].map((index) => assignment({
      operation_id: `SO-001-R001-B${String(index + 1).padStart(3, '0')}-OP10`,
      changeover_start: new Date(Date.parse(`${day}T02:00:00Z`) + index * 600_000).toISOString(),
      start_at: new Date(Date.parse(`${day}T02:00:00Z`) + index * 600_000).toISOString(),
      end_at: new Date(Date.parse(`${day}T02:00:00Z`) + (index + 1) * 600_000).toISOString(),
    }))
    view(workspace(snapshot, [candidate(dense)]))
    expect(screen.getByRole('button', { name: 'SO-001 · OP10 · 30 batches Planned' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^B001·OP10/ })).not.toBeInTheDocument()

    // Asking explicitly for each batch restores single operations.
    await userEvent.click(screen.getByText('Display settings'))
    expect(screen.getByRole('dialog', { name: 'Display settings' })).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Show each batch' }))
    await userEvent.click(screen.getByRole('button', { name: 'Close display settings' }))
    expect(screen.queryByRole('dialog', { name: 'Display settings' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'B001·OP10 Planned' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /30 batches/ })).not.toBeInTheDocument()
  })

  it('opening a merged bar lists its batches and leads on to single operations', async () => {
    const snapshot = boardSnapshot()
    const pair = [
      assignment({ operation_id: 'SO-001-R001-B001-OP10', changeover_start: `${day}T02:00:00Z`, start_at: `${day}T02:00:00Z`, end_at: `${day}T02:10:00Z` }),
      assignment({ operation_id: 'SO-001-R001-B002-OP10', changeover_start: `${day}T02:10:00Z`, start_at: `${day}T02:10:00Z`, end_at: `${day}T02:20:00Z` }),
    ]
    const { navigate } = view(workspace(snapshot, [candidate(pair)]))
    await userEvent.click(screen.getByText('Display settings'))
    await userEvent.click(screen.getByRole('button', { name: 'Merge similar operations' }))
    await userEvent.click(screen.getByRole('button', { name: 'Close display settings' }))
    await userEvent.click(screen.getByRole('button', { name: 'SO-001 · OP10 · 2 batches Planned' }))
    const drawer = screen.getByRole('dialog', { name: /Consecutive operations/ })
    expect(within(drawer).getByText('2 consecutive batches')).toBeVisible()
    expect(within(drawer).getByText('10:00 → 10:20')).toBeVisible()
    expect(within(drawer).getByText('Total quantity')).toBeVisible()
    expect(within(drawer).getByRole('heading', { name: 'SO-001-R001-B001' })).toBeVisible()
    await userEvent.click(within(drawer).getAllByRole('button', { name: 'View this operation' })[0]!)
    expect(navigate).toHaveBeenCalledWith({ operationId: 'SO-001-R001-B001-OP10' })
  })

  it('the time axis can zoom in and scrolls horizontally when zoomed', async () => {
    view(workspace(boardSnapshot(), [candidate(plan)]))
    await userEvent.click(screen.getByText('Display settings'))
    const zoomGroup = screen.getByRole('group', { name: 'Time axis width' })
    expect(within(zoomGroup).getByRole('button', { name: 'Fit width' })).toHaveAttribute('aria-pressed', 'true')
    await userEvent.click(within(zoomGroup).getByRole('button', { name: '4×' }))
    expect(within(zoomGroup).getByRole('button', { name: '4×' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('the legend lists all states and shift meanings', async () => {
    view(workspace(boardSnapshot(), [candidate(plan)]))
    await userEvent.click(screen.getByText('Display settings'))
    expect(screen.getByText('Scheduled; the factory has no execution record yet')).toBeVisible()
    expect(screen.getByText('Overtime window in the calendar; needs manager approval')).toBeVisible()
    expect(screen.getByText('Gaps between calendar windows, including breaks and nights')).toBeVisible()
    expect(screen.getByText(/The thick upper bar is the plan, the thin lower bar the actual execution segments/)).toBeVisible()
  })
})
