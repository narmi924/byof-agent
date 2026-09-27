import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from './api'
import { SimulatorPanel } from './SimulatorPanel'
import { actual, boardSnapshot } from './test/boardFixture'
import { businessTerms } from './test/businessFixture'

vi.mock('./api', async importOriginal => ({ ...await importOriginal<typeof import('./api')>(), readSimulator: vi.fn(), readRecoveryRequests: vi.fn(), todayRunSimulator: vi.fn(), commandSimulator: vi.fn(), replaySimulator: vi.fn() }))

beforeEach(() => {
  sessionStorage.clear()
  const snapshot = boardSnapshot()
  vi.mocked(api.readSimulator).mockResolvedValue({ factory_id: snapshot.factory_id, run_id: snapshot.run_id!, mode: 'PAUSED', interval_ms: 5000, business_clock: snapshot.snapshot_clock })
  vi.mocked(api.readRecoveryRequests).mockResolvedValue({ run_id: snapshot.run_id!, requests: [] })
  vi.mocked(api.todayRunSimulator).mockResolvedValue(undefined)
  vi.mocked(api.commandSimulator).mockResolvedValue(undefined)
  vi.mocked(api.replaySimulator).mockResolvedValue(undefined)
})

it('the disruption simulator no longer takes over manual tasks of the manager response plan', async () => {
  const source = boardSnapshot()
  vi.mocked(api.readRecoveryRequests).mockResolvedValue({ run_id: source.run_id!, requests: [{ action_id: 'recovery-1', created_at: source.snapshot_clock, state: 'AWAITING_SOURCE', path: { path_id: 'supply-1', kind: 'material_supply', title: 'Cover the verified material shortfall', evidence: 'Seals short by at least 50 EA', steps: [{ owner: 'Manager', action: 'Choose the confirmed supply.' }, { owner: 'Shop floor', action: 'Enter the confirmed receipt.' }], prompt: 'Please continue.' } }] })
  mount(source)
  expect(await screen.findByRole('region', { name: 'Disruption simulator' })).not.toHaveTextContent('no need to come back here')
  expect(api.readRecoveryRequests).not.toHaveBeenCalled()
  expect(screen.queryByRole('region', { name: 'Recovery items chosen by the manager' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /approv/i })).not.toBeInTheDocument()
})

import type { Snapshot } from './contracts'

function mount(snapshot = boardSnapshot(), onChanged = vi.fn().mockResolvedValue(undefined)) {
  const props = { factoryId: snapshot.factory_id, userId: 'maintainer', onChanged, onSessionEnded: vi.fn() }
  const view = render(<SimulatorPanel {...props} snapshot={snapshot} />)
  return { ...view, update: (next: Snapshot) => view.rerender(<SimulatorPanel {...props} snapshot={next} />) }
}
async function open(label: string, name: string) {
  const button = screen.getByRole('button', { name: label === 'Demo controls' ? 'Demo tools' : label })
  await waitFor(() => expect(button).toBeEnabled())
  await userEvent.click(button)
  return screen.getByRole('form', { name })
}
/** Record editing forms; the persistent run controls are not part of any record. */
const editorForms = () => screen.queryAllByRole('form').filter(form => !form.closest('.sim-controls'))
async function saved() { await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument()) }
async function close() {
  await userEvent.click(screen.getByRole('button', { name: 'Close editor' }))
  const discard = screen.queryByRole('button', { name: 'Discard and close' })
  if (discard) await userEvent.click(discard)
}
function change(form: HTMLElement, name: string | RegExp, value: string) {
  fireEvent.change(within(form).getByLabelText(name), { target: { value } })
}
const receipt = { receipt_id: 'RCPT-1', material_id: 'MAT-RING', quantity: 100, unit: 'EA', eta: '2026-09-14T08:00:00Z', status: 'CONFIRMED', received_at: null, version: 1 } as const

it('shows shop floor data by default; choosing a record mounts only its form and shows no manager plan approval', async () => {
  mount(boardSnapshot({ receipts: [receipt], business_terms: businessTerms }))
  expect(editorForms()).toHaveLength(0)
  expect(screen.getByRole('heading', { name: 'Shop floor' })).toBeVisible()
  expect(screen.queryByRole('navigation', { name: 'Shop floor action navigation' })).not.toBeInTheDocument()
  await open('Change order SO-001', 'Change order')
  expect(editorForms()).toHaveLength(1)
  expect(screen.getByRole('dialog', { name: 'Order SO-001' })).toBeVisible()
  expect(screen.queryByRole('combobox', { name: 'Order' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Compare rush order|Compare shortage|Choose plan|Confirm and record business terms/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: /overtime|cost/i })).not.toBeInTheDocument()
  await close()
  expect(api.commandSimulator).not.toHaveBeenCalled()
})

it('an old administrator business acceptance pending record is neither resent nor overwritten', async () => {
  const snapshot = boardSnapshot()
  const key = `byof.pending.v1:maintainer:${snapshot.factory_id}`
  const pending = JSON.stringify({ version: 1, userId: 'maintainer', factoryId: snapshot.factory_id, slots: { business: { request_id: 'old-accept', run_id: snapshot.run_id, payload: { job_id: 'study-old', option_id: 'option-1', study_hash: 'a'.repeat(64), confirm_extra_cost: true, allow_overtime: true } } } })
  sessionStorage.setItem(key, pending)
  render(<SimulatorPanel factoryId={snapshot.factory_id} userId="maintainer" snapshot={snapshot} onChanged={vi.fn().mockResolvedValue(undefined)} onSessionEnded={vi.fn()} />)
  expect(await screen.findByText(/The old option comparison or acceptance entry has been retired/)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Check the original request' })).not.toBeInTheDocument()
  expect(api.commandSimulator).not.toHaveBeenCalled()
  expect(sessionStorage.getItem(key)).toBe(pending)
  await userEvent.click(screen.getByRole('button', { name: 'Keep old records and continue' }))
  expect(screen.queryByRole('button', { name: 'Keep old records and continue' })).not.toBeInTheDocument()
  expect(screen.queryByText(/The old option comparison or acceptance entry has been retired/)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Change order SO-001' })).toBeEnabled()
  const archive = Object.keys(sessionStorage).find(item => item.startsWith(`${key}:retired-business:`))
  expect(archive).toBeDefined()
  expect(sessionStorage.getItem(archive!)).toBe(pending)
  expect(api.commandSimulator).not.toHaveBeenCalled()
})


it('stock counts, confirmed receipts and supply changes start from data rows and keep versions and the factory time zone', async () => {
  const snapshot = boardSnapshot({ receipts: [receipt], inventory: [{ ...boardSnapshot().inventory[0]!, version: 3 }] })
  mount(snapshot)
  let form = await open('Count MAT-RING', 'Physical stock count')
  change(form, /Counted physical quantity/, '120')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm stock count' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'inventory.reconcile', payload: { material_id: 'MAT-RING', expected_version: 3, counted_on_hand: 120, reason: 'COUNT_CORRECTION' } }), expect.any(AbortSignal))
  form = await open('Record receipt', 'Record a confirmed receipt')
  change(form, 'Receipt ID', 'INB-NEW'); change(form, 'Confirmed quantity', '80'); change(form, /Expected arrival/, '2026-09-14T11:15')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm receipt' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'receipt.add', payload: { receipt_id: 'INB-NEW', material_id: 'MAT-RING', quantity: 80, eta: '2026-09-14T03:15:00.000Z' } }), expect.any(AbortSignal))
  form = await open('Update receipt RCPT-1', 'Update receipt')
  await userEvent.selectOptions(within(form).getByRole('combobox', { name: 'Receipt change' }), 'shortfall')
  change(form, 'New expected quantity', '20')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm receipt change' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'receipt.shortfall', payload: { receipt_id: 'RCPT-1', quantity: 20 } }), expect.any(AbortSignal))
  form = await open('Update receipt RCPT-1', 'Update receipt')
  await userEvent.selectOptions(within(form).getByRole('combobox', { name: 'Receipt change' }), 'cancel')
  await userEvent.click(within(form).getByRole('checkbox'))
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm receipt change' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'receipt.cancel', payload: { receipt_id: 'RCPT-1' } }), expect.any(AbortSignal))
})

it('a new rush order and a change to an existing order bind separately and return to the shop floor list after saving', async () => {
  const snapshot = boardSnapshot({ orders: [{ ...boardSnapshot().orders[0]!, version: 2 }] })
  mount(snapshot)
  let form = await open('New order', 'New order')
  change(form, 'Order ID', 'URG-01'); change(form, 'Order quantity (pcs)', '50'); change(form, /^Due \(/, '2026-09-14T16:00')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm new order' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'order.add', payload: expect.objectContaining({ order_id: 'URG-01', due_at: '2026-09-14T08:00:00.000Z' }) }), expect.any(AbortSignal))
  form = await open('Change order SO-001', 'Change order')
  change(form, /Demand quantity/, '150'); change(form, /^Due \(/, '2026-09-14T16:00')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm order change' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'order.revise', payload: expect.objectContaining({ order_id: 'SO-001', expected_version: 2, quantity: 150 }) }), expect.any(AbortSignal))
  const log = screen.getByRole('region', { name: 'Disruptions this run' })
  expect(log).toHaveTextContent('Order changed · SO-001 → 150 pcs, due 09-14 16:00')
  expect(log).toHaveTextContent('Order added · URG-01 · 50 pcs')
})

it.each(['IN_PROGRESS', 'COMPLETED'] as const)('a %s order can be cancelled and still says WIP continues and qualified surplus goes to stock', async status => {
  const snapshot = boardSnapshot({ orders: [{ ...boardSnapshot().orders[0]!, status, version: 4 }] })
  mount(snapshot)
  const form = await open('Change order SO-001', 'Change order')
  expect(within(form).getByText(/started batches still finish/)).toBeVisible()
  expect(within(form).getByText(status === 'IN_PROGRESS' ? /Order: SO-001 · In production/ : /Order: SO-001 · Production completed/)).toBeVisible()
  change(form, /Demand quantity/, '0')
  await userEvent.click(within(form).getByRole('checkbox', { name: /Confirm cancelling .* SO-001/ }))
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm order change' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'order.revise', payload: expect.objectContaining({ expected_version: 4, quantity: 0 }) }), expect.any(AbortSignal))
})

it('choosing the original order again after cancelling restores demand on the synced new version', async () => {
  const snapshot = boardSnapshot({ orders: [{ ...boardSnapshot().orders[0]!, version: 6 }] })
  const view = mount(snapshot)
  let form = await open('Change order SO-001', 'Change order')
  expect(within(form).getByText(/Enter a positive number to restore a cancelled order/)).toBeVisible()
  change(form, /Demand quantity/, '0')
  await userEvent.click(within(form).getByRole('checkbox', { name: /Confirm cancelling .* SO-001/ }))
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm order change' }))
  await saved()
  view.update(boardSnapshot({ orders: [{ ...snapshot.orders[0]!, status: 'CANCELLED', quantity: 0, version: 7 }] }))
  form = await open('Change order SO-001', 'Change order')
  expect(within(form).getByText(/Order: SO-001 · Cancelled · 0 pcs/)).toBeVisible()
  change(form, /Demand quantity/, '50')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm order change' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'order.revise', payload: expect.objectContaining({ order_id: 'SO-001', expected_version: 7, quantity: 50 }) }), expect.any(AbortSignal))
})

it('a background version change keeps the draft and blocks applying it to the new version until an explicit reload', async () => {
  const snapshot = boardSnapshot()
  const view = mount(snapshot)
  let form = await open('Change order SO-001', 'Change order')
  change(form, /Demand quantity/, '150')
  view.update(boardSnapshot({ orders: [{ ...snapshot.orders[0]!, quantity: 200, version: 2 }] }))
  expect(within(form).getByLabelText(/Demand quantity/)).toHaveValue(150)
  expect(within(form).getByRole('button', { name: 'Confirm order change' })).toBeDisabled()
  fireEvent.submit(form)
  expect(api.commandSimulator).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Discard input and load the latest data' }))
  form = screen.getByRole('form', { name: 'Change order' })
  expect(within(form).getByLabelText(/Demand quantity/)).toHaveValue(200)
  change(form, /Demand quantity/, '250')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm order change' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ payload: expect.objectContaining({ expected_version: 2, quantity: 250 }) }), expect.any(AbortSignal))
})

it('a vanished order does not drift to another order and stays unsubmittable after reloading', async () => {
  const snapshot = boardSnapshot()
  const view = mount(snapshot)
  const form = await open('Change order SO-001', 'Change order')
  change(form, /Demand quantity/, '150')
  view.update(boardSnapshot({ orders: [{ ...snapshot.orders[0]!, order_id: 'SO-OTHER' }] }))
  expect(within(form).getByRole('button', { name: 'Confirm order change' })).toBeDisabled()
  expect(within(form).getByText(/Order: SO-001/)).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Discard input and load the latest data' }))
  expect(screen.queryByRole('form', { name: 'Change order' })).not.toBeInTheDocument()
  expect(screen.getByRole('dialog')).toHaveAccessibleName('Order SO-001')
  expect(api.commandSimulator).not.toHaveBeenCalled()
})

it('when the source confirmed but the data refresh failed it stays in the editor, asks to check and does not resend', async () => {
  mount(boardSnapshot(), vi.fn().mockRejectedValue(new Error('Read failed')))
  const form = await open('Change order SO-001', 'Change order')
  change(form, /Demand quantity/, '0')
  await userEvent.click(within(form).getByRole('checkbox', { name: /Confirm cancelling .* SO-001/ }))
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm order change' }))
  expect(await screen.findByText('Order change confirmed.')).toBeVisible()
  expect(await screen.findByText(/The shop floor data did not refresh/)).toBeVisible()
  expect(screen.getByRole('dialog')).toBeVisible()
  expect(screen.queryByText(/synced|generated.*suggestion/i)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Check the original request' })).not.toBeInTheDocument()
  await close()
  expect(screen.getByRole('heading', { name: 'Orders and delivery' })).toBeVisible()
  expect(api.commandSimulator).toHaveBeenCalledOnce()
})

it('a new rule reuses the original idempotent command and returns to the list once the data refresh finishes', async () => {
  let finishReading!: () => void
  mount(boardSnapshot(), vi.fn(() => new Promise<void>(resolve => { finishReading = resolve })))
  const form = await open('Split-delivery rule BRG-6202', 'Customer split-delivery rule')
  await userEvent.click(within(form).getByRole('checkbox'))
  await userEvent.click(within(form).getByRole('button', { name: 'Save split-delivery rule' }))
  expect(await screen.findByText('Refreshing the shop floor.')).toBeVisible()
  expect(within(form).getByRole('button', { name: 'Save split-delivery rule' })).toBeDisabled()
  expect(screen.getByRole('dialog')).toBeVisible()
  await act(async () => finishReading())
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith('skf-workshop', expect.objectContaining({ request_id: expect.any(String), run_id: 'run-1', kind: 'delivery_rule.set', payload: expect.objectContaining({ expected_terms_version: null, partial_delivery_allowed: true }) }), expect.any(AbortSignal))
})

it('after closing the editor an unknown result can still recover the original request, and duplicate writes across forms are blocked', async () => {
  vi.mocked(api.commandSimulator).mockRejectedValue(new Error('Network unavailable'))
  mount(boardSnapshot({ business_terms: businessTerms }))
  const form = await open('Split-delivery rule BRG-6202', 'Customer split-delivery rule')
  await userEvent.click(within(form).getByRole('button', { name: 'Save split-delivery rule' }))
  await screen.findByRole('button', { name: 'Check the original request' })
  const first = vi.mocked(api.commandSimulator).mock.calls[0]![1]
  fireEvent.submit(form)
  expect(api.commandSimulator).toHaveBeenCalledOnce()
  await close()
  expect(screen.getByRole('button', { name: 'New order' })).toBeDisabled()
  expect(editorForms()).toHaveLength(0)
  await userEvent.click(screen.getByRole('button', { name: 'Check the original request' }))
  await waitFor(() => expect(api.commandSimulator).toHaveBeenCalledTimes(2))
  expect(vi.mocked(api.commandSimulator).mock.calls[1]![1]).toEqual(first)
})

it('a replay can view the selected conditions, but split-delivery rules, quotes and overtime are never writable', async () => {
  const base = boardSnapshot()
  mount(boardSnapshot({ receipts: [receipt], business_terms: businessTerms, source: { ...base.source, source_system: 'factory-simulator-replay' }, workers: base.workers.map(item => ({ ...item, version: 1 })) }))
  for (const [entry, form, buttons] of [
    ['Split-delivery rule BRG-6202', 'Customer split-delivery rule', ['Save split-delivery rule']],
    ['Edit quote Q1', 'Supplier expedite quote', ['Save expedite quote', 'Delete this quote']],
    ['Overtime windows worker W01', 'Overtime availability of machines and staff', ['Record overtime availability', 'Remove this overtime window']],
  ] as const) {
    await open(entry, form)
    for (const name of buttons) expect(screen.getByRole('button', { name })).toBeDisabled()
    await close()
  }
  expect(api.commandSimulator).not.toHaveBeenCalled()
})

it('the clock cannot start before a plan is active; it starts only after manager approval', async () => {
  const snapshot = boardSnapshot()
  vi.mocked(api.readSimulator).mockResolvedValue({ factory_id: snapshot.factory_id, run_id: snapshot.run_id!, mode: 'PAUSED', interval_ms: 60000, business_clock: snapshot.snapshot_clock, server_time: snapshot.snapshot_clock })
  mount(snapshot)
  const controls = screen.getByRole('region', { name: 'Run controls' })
  expect(await within(controls).findByText("Starts automatically once the manager approves today's plan")).toBeVisible()
  expect(within(controls).getByRole('button', { name: 'Run' })).toBeDisabled()
  expect(within(controls).getByRole('button', { name: 'Advance' })).toBeDisabled()
})

it('the run control bar stays at the top with advance, run and reset today; random breakdowns and replay live under More', async () => {
  const snapshot = boardSnapshot({ active_plan_version: 'plan-1' })
  vi.mocked(api.readSimulator).mockResolvedValue({ factory_id: snapshot.factory_id, run_id: snapshot.run_id!, mode: 'PAUSED', interval_ms: 60000, business_clock: snapshot.snapshot_clock, server_time: snapshot.snapshot_clock })
  mount(snapshot)
  const controls = screen.getByRole('region', { name: 'Run controls' })
  await waitFor(() => expect(within(controls).getByRole('button', { name: 'Advance' })).toBeEnabled())
  expect(within(controls).getByRole('button', { name: 'Run' })).toBeEnabled()
  expect(within(controls).queryByText("Starts automatically once the manager approves today's plan")).not.toBeInTheDocument()
  await userEvent.click(within(controls).getByRole('button', { name: 'More' }))
  expect(screen.getByRole('button', { name: 'Turn on random breakdowns' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Replay this run' })).toBeEnabled()
  await userEvent.click(screen.getByRole('button', { name: 'Close' }))
  await userEvent.click(within(controls).getByRole('button', { name: 'Reset today' }))
  expect(screen.getByText(/Earlier runs are kept/)).toBeVisible()
  expect(screen.queryByText(/scenario|v2/i)).not.toBeInTheDocument()
  expect(api.todayRunSimulator).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Confirm reset' }))
  await waitFor(() => expect(api.todayRunSimulator).toHaveBeenCalledWith(snapshot.factory_id, expect.objectContaining({ expected_run_id: snapshot.run_id, scenario_version: 'workshop-full-2' }), expect.any(AbortSignal)))
})

it('while running it can only pause or change speed, not advance step by step', async () => {
  const snapshot = boardSnapshot({ active_plan_version: 'plan-1' })
  vi.mocked(api.readSimulator).mockResolvedValue({ factory_id: snapshot.factory_id, run_id: snapshot.run_id!, mode: 'RUNNING', interval_ms: 15000, business_clock: snapshot.snapshot_clock })
  mount(snapshot)
  const controls = screen.getByRole('region', { name: 'Run controls' })
  await userEvent.click(await within(controls).findByRole('button', { name: 'Pause' }))
  expect(within(controls).getByRole('button', { name: 'Advance' })).toBeDisabled()
  await waitFor(() => expect(api.commandSimulator).toHaveBeenCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'clock.pause', run_id: snapshot.run_id }), expect.any(AbortSignal)))
})

it('after a demo action is confirmed all clock inputs are locked during the refresh to prevent duplicate submissions', async () => {
  let finishReading!: () => void
  mount(boardSnapshot({ active_plan_version: 'plan-1' }), vi.fn(() => new Promise<void>(resolve => { finishReading = resolve })))
  const controls = screen.getByRole('region', { name: 'Run controls' })
  const step = within(controls).getByRole('button', { name: 'Advance' })
  await waitFor(() => expect(step).toBeEnabled())
  await userEvent.click(step)
  expect(await screen.findByText('Refreshing the shop floor.')).toBeVisible()
  expect(within(controls).getByRole('spinbutton')).toBeDisabled()
  expect(within(controls).getByRole('button', { name: 'Run' })).toBeDisabled()
  expect(within(controls).getByRole('button', { name: 'More' })).toBeDisabled()
  fireEvent.submit(within(controls).getByRole('form', { name: 'Advance step by step' }))
  expect(api.commandSimulator).toHaveBeenCalledOnce()
  await act(async () => finishReading())
})

it('temporary machine stops, temporary leave and absence, remaining work and quality checks still bind to the right rows', async () => {
  const snapshot = boardSnapshot({ actuals: [actual({ operation_id: 'SO-001-R001-B001:OP10', state: 'BLOCKED' }), actual({ operation_id: 'SO-001-R001-B001:OP20', state: 'COMPLETED' })] })
  mount(snapshot)
  let form = await open('Edit machine KIT-01', 'Machine status')
  await userEvent.selectOptions(within(form).getByRole('combobox', { name: 'Machine action' }), 'outage')
  change(form, /Stop duration/, '45')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm machine status' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'resource.outage', payload: { resource_id: 'KIT-01', minutes: 45 } }), expect.any(AbortSignal))
  form = await open('Edit worker W02', 'Worker status')
  change(form, /Leave duration/, '60')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm worker status' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'worker.leave', payload: { worker_id: 'W02', minutes: 60 } }), expect.any(AbortSignal))
  form = await open('Edit worker W02', 'Worker status')
  await userEvent.selectOptions(within(form).getByRole('combobox', { name: 'Worker action' }), 'absent')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm worker status' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'worker.absent', payload: { worker_id: 'W02' } }), expect.any(AbortSignal))
  form = await open('Confirm remaining SO-001-R001-B001:OP10', 'Confirm remaining work after the interruption')
  change(form, 'Remaining production minutes', '10'); change(form, 'Remaining changeover minutes', '0')
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm remaining work' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'execution.confirm_remaining', payload: { operation_id: 'SO-001-R001-B001:OP10', remaining_minutes: 10, remaining_setup_minutes: 0 } }), expect.any(AbortSignal))
  form = await open('Record quality SO-001-R001-B001:OP20', 'Quality check result')
  await userEvent.selectOptions(within(form).getByRole('combobox', { name: 'Check result' }), 'PASSED')
  await userEvent.click(within(form).getByRole('button', { name: 'Record result' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'quality.record', payload: { operation_id: 'SO-001-R001-B001:OP20', quality_state: 'PASSED' } }), expect.any(AbortSignal))
})

it('a failed batch can be scrapped on shop floor evidence and remade', async () => {
  const snapshot = boardSnapshot({ actuals: [actual({ operation_id: 'SO-001-R001-B001:OP20', state: 'COMPLETED', quality_state: 'FAILED' })] })
  mount(snapshot)
  await open('Record quality SO-001-R001-B001:OP20', 'Quality check result')
  const form = await screen.findByRole('form', { name: 'Unrecoverable batch' })
  await userEvent.type(within(form).getByRole('textbox', { name: 'Scrap evidence' }), 'Recheck confirmed not repairable')
  await userEvent.click(within(form).getByRole('checkbox', { name: /Confirm the batch cannot be recovered/ }))
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm scrap and remake' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith(snapshot.factory_id, expect.objectContaining({ kind: 'quality.scrap', payload: { operation_id: 'SO-001-R001-B001:OP20', reason: 'Recheck confirmed not repairable' } }), expect.any(AbortSignal))
})

it('an orphaned old quote can be deleted but cannot be rebound to another receipt and saved', async () => {
  mount(boardSnapshot({ business_terms: businessTerms }))
  const form = await open('Edit quote Q1', 'Supplier expedite quote')
  expect(within(form).getByRole('button', { name: 'Save expedite quote' })).toBeDisabled()
  await userEvent.click(within(form).getByRole('button', { name: 'Delete this quote' }))
  await userEvent.click(within(form).getByRole('button', { name: 'Confirm delete' }))
  await saved()
  expect(api.commandSimulator).toHaveBeenLastCalledWith('skf-workshop', expect.objectContaining({ kind: 'expedite_quote.remove', payload: { expected_terms_version: 'business-v1', quote_id: 'Q1' } }), expect.any(AbortSignal))
})
