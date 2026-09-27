import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { FactoryBusinessEditor } from './FactoryBusinessEditor'
import { boardSnapshot } from '../test/boardFixture'
import { businessTerms } from '../test/businessFixture'
import type { Snapshot } from '../contracts'
import type { FactoryEditor } from '../factoryEditor'
type Selection = Extract<FactoryEditor, { kind: 'delivery-rule' | 'quote' | 'overtime' }>
const newQuote: Selection = { kind: 'quote', receiptId: 'RCPT-1' }
const oldQuote: Selection = { kind: 'quote', receiptId: 'RCPT-1', quoteId: 'Q1' }
const worker: Selection = { kind: 'overtime', targetType: 'worker', targetId: 'W01' }

function setup(overrides: Partial<Snapshot> = {}, disabled = false, selection: Selection = { kind: 'delivery-rule', productId: 'BRG-6202' }) {
  const base = boardSnapshot()
  const snapshot = boardSnapshot({
    workers: base.workers.map(item => ({ ...item, version: 3 })),
    resources: base.resources.map(item => ({ ...item, version: 4 })),
    receipts: [{ receipt_id: 'RCPT-1', material_id: 'MAT-RING', quantity: 100, unit: 'EA', eta: '2026-09-14T08:00:00Z', status: 'CONFIRMED', received_at: null, version: 1 }],
    ...overrides,
  })
  const command = vi.fn(), onError = vi.fn()
  const props = { snapshot, disabled, command, onError, zone: snapshot.profile.timezone, status: null }
  const view = render(<FactoryBusinessEditor {...props} selection={selection} />)
  return { snapshot, command, onError, choose: (next: Selection) => view.rerender(<FactoryBusinessEditor key={JSON.stringify(next)} {...props} selection={next} />) }
}

function fillQuoteTimes(form: HTMLElement) {
  fireEvent.change(within(form).getByLabelText(/Expedited expected arrival/), { target: { value: '2026-09-14T13:00' } })
  // Validity may extend past original ETA; the source only requires future validity.
  fireEvent.change(within(form).getByLabelText(/Quote valid until/), { target: { value: '2026-09-14T18:00' } })
}

it('an unconfigured rule needs the administrator to allow split delivery explicitly and carries the empty source version and real SKU batch size', async () => {
  const { command } = setup()
  const form = screen.getByRole('form', { name: 'Customer split-delivery rule' })
  expect(within(form).getByText(/No split-delivery rule is configured/)).toBeVisible()
  expect(within(form).getByRole('checkbox')).not.toBeChecked()
  expect(within(form).getByRole('spinbutton')).toHaveValue(50)
  await userEvent.click(within(form).getByRole('checkbox'))
  await userEvent.click(within(form).getByRole('button', { name: 'Save split-delivery rule' }))
  expect(command).toHaveBeenCalledWith('delivery_rule.set', { expected_terms_version: null, product_id: 'BRG-6202', partial_delivery_allowed: true, minimum_partial_quantity: 50, max_deliveries: 2 }, expect.any(String))
})

it('an existing rule shows its values and carries the source version; split quantities that are not whole batches are rejected', async () => {
  const { command, onError } = setup({ business_terms: businessTerms })
  const form = screen.getByRole('form', { name: 'Customer split-delivery rule' })
  expect(within(form).getByText(/Current rule: split delivery allowed/)).toBeVisible()
  fireEvent.change(within(form).getByRole('spinbutton'), { target: { value: '75' } })
  fireEvent.submit(form)
  expect(onError).toHaveBeenCalledWith(expect.stringContaining('whole multiple'))
  expect(command).not.toHaveBeenCalled()
  fireEvent.change(within(form).getByRole('spinbutton'), { target: { value: '100' } })
  await userEvent.click(within(form).getByRole('checkbox'))
  await userEvent.click(within(form).getByRole('button', { name: 'Save split-delivery rule' }))
  expect(command).toHaveBeenCalledWith('delivery_rule.set', expect.objectContaining({ expected_terms_version: 'business-v1', partial_delivery_allowed: false, minimum_partial_quantity: 100 }), expect.any(String))
})

it('a new quote has no sample cost; an explicit unknown is saved as null and the receipt facts are not faked by the form', async () => {
  const { command } = setup({}, false, newQuote)
  const form = screen.getByRole('form', { name: 'Supplier expedite quote' })
  expect(within(form).getByRole('spinbutton', { name: /Extra cost/ })).toHaveValue(null)
  expect(within(form).getByRole('combobox', { name: 'Currency' })).toHaveValue('')
  await userEvent.type(within(form).getByRole('textbox', { name: 'Quote ID' }), 'Q-NEW')
  await userEvent.type(within(form).getByRole('textbox', { name: 'Source reference or supplier quote ID' }), 'supplier-verified')
  fillQuoteTimes(form)
  await userEvent.click(within(form).getByRole('checkbox', { name: 'Quote amount not confirmed yet' }))
  await userEvent.click(within(form).getByRole('button', { name: 'Save expedite quote' }))
  expect(command).toHaveBeenCalledWith('expedite_quote.set', {
    expected_terms_version: null, quote_id: 'Q-NEW', receipt_id: 'RCPT-1', expected_receipt_version: 1,
    expedited_eta: '2026-09-14T05:00:00.000Z', valid_until: '2026-09-14T10:00:00.000Z',
    source_reference: 'supplier-verified', cost_minor: null, currency: null,
  }, expect.any(String))
})

it('an entered quote converts a two-decimal amount exactly to minor units and rejects an empty cost treated as zero', async () => {
  const { command, onError } = setup({}, false, newQuote)
  const form = screen.getByRole('form', { name: 'Supplier expedite quote' })
  await userEvent.type(within(form).getByRole('textbox', { name: 'Quote ID' }), 'Q-NEW')
  await userEvent.type(within(form).getByRole('textbox', { name: 'Source reference or supplier quote ID' }), 'ref-1')
  fillQuoteTimes(form)
  fireEvent.submit(form)
  expect(onError).toHaveBeenCalledWith(expect.stringContaining('tick the box for an unknown cost'))
  expect(command).not.toHaveBeenCalled()
  await userEvent.type(within(form).getByRole('spinbutton', { name: /Extra cost/ }), '12.34')
  await userEvent.selectOptions(within(form).getByRole('combobox', { name: 'Currency' }), 'CNY')
  await userEvent.click(within(form).getByRole('button', { name: 'Save expedite quote' }))
  expect(command).toHaveBeenCalledWith('expedite_quote.set', expect.objectContaining({ cost_minor: 1234, currency: 'CNY' }), expect.any(String))
})

it('a quote selection shows why the receipt version invalidated it; deleting carries only the quote and terms version', async () => {
  const { command } = setup({ business_terms: { ...businessTerms, expedite_quotes: [{ ...businessTerms.expedite_quotes[0]!, receipt_version: 2 }] } }, false, oldQuote)
  expect(screen.getByText(/The receipt quantity, date or version has changed/)).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Delete this quote' }))
  expect(command).not.toHaveBeenCalled()
  await userEvent.click(screen.getByRole('button', { name: 'Confirm delete' }))
  expect(command).toHaveBeenCalledWith('expedite_quote.remove', { expected_terms_version: 'business-v1', quote_id: 'Q1' }, expect.any(String))
})

it('quote timestamps in different formats but the same instant still show a valid match', async () => {
  setup({ business_terms: { ...businessTerms, expedite_quotes: [{ ...businessTerms.expedite_quotes[0]!, original_eta: '2026-09-14T08:00:00+00:00' }] } }, false, oldQuote)
  expect(screen.getByText(/The quote still matches the current receipt/)).toBeVisible()
})

it('staff overtime entry shows the factory clock and the manager approval boundary and uses the current version of the worker or machine', async () => {
  const { command, choose } = setup({}, false, worker)
  let form = screen.getByRole('form', { name: 'Overtime availability of machines and staff' })
  expect(within(form).getByText(/Current factory time: .*Allowed range/)).toBeVisible()
  expect(within(form).getByText(/overtime in a plan still needs manager approval/)).toBeVisible()
  fireEvent.change(within(form).getByLabelText(/Overtime start/), { target: { value: '2026-09-15T17:30' } })
  fireEvent.change(within(form).getByLabelText(/Overtime end/), { target: { value: '2026-09-15T19:30' } })
  await userEvent.click(within(form).getByRole('button', { name: 'Record overtime availability' }))
  expect(command).toHaveBeenCalledWith('overtime_window.set', { target_type: 'worker', target_id: 'W01', expected_version: 3, action: 'add', start_at: '2026-09-15T09:30:00.000Z', end_at: '2026-09-15T11:30:00.000Z' }, expect.any(String))
  choose({ kind: 'overtime', targetType: 'resource', targetId: 'KIT-01' })
  form = screen.getByRole('form', { name: 'Overtime availability of machines and staff' })
  await userEvent.click(within(form).getByRole('button', { name: 'Remove this overtime window' }))
  expect(command).toHaveBeenLastCalledWith('overtime_window.set', { target_type: 'resource', target_id: 'KIT-01', expected_version: 4, action: 'remove', start_at: '2026-09-14T09:30:00Z', end_at: '2026-09-14T11:30:00Z' }, expect.any(String))
  expect(within(form).getAllByText(/Regular shift \(read-only\)/)).toHaveLength(2)
})

it('overtime windows reject overlaps and input outside the scheduling window; a missing version cannot fake a save', async () => {
  const { command, onError, choose } = setup({ workers: boardSnapshot().workers }, false, worker)
  let form = screen.getByRole('form', { name: 'Overtime availability of machines and staff' })
  expect(within(form).getByRole('button', { name: 'Record overtime availability' })).toBeDisabled()
  expect(within(form).getByRole('button', { name: 'Remove this overtime window' })).toBeDisabled()
  choose({ kind: 'overtime', targetType: 'resource', targetId: 'KIT-01' })
  form = screen.getByRole('form', { name: 'Overtime availability of machines and staff' })
  fireEvent.change(within(form).getByLabelText(/Overtime start/), { target: { value: '2026-09-14T11:00' } })
  fireEvent.change(within(form).getByLabelText(/Overtime end/), { target: { value: '2026-09-14T12:00' } })
  fireEvent.submit(form)
  expect(onError).toHaveBeenLastCalledWith(expect.stringContaining('overlaps'))
  fireEvent.change(within(form).getByLabelText(/Overtime start/), { target: { value: '2026-09-17T17:30' } })
  fireEvent.change(within(form).getByLabelText(/Overtime end/), { target: { value: '2026-09-17T19:30' } })
  fireEvent.submit(form)
  expect(onError).toHaveBeenLastCalledWith(expect.stringContaining('scheduling window'))
  expect(command).not.toHaveBeenCalled()
})

it('enterprise-sourced rules and quotes cannot be overwritten from the simulator; pending or replay states disable all new writes', async () => {
  const enterprise = setup({ business_terms: { ...businessTerms, evidence_mode: 'enterprise' } })
  expect(screen.getByRole('button', { name: 'Save split-delivery rule' })).toBeDisabled()
  enterprise.choose(oldQuote)
  expect(screen.getByRole('button', { name: 'Save expedite quote' })).toBeDisabled()
  expect(enterprise.command).not.toHaveBeenCalled()
})

it('a disabled parent blocks writes of the selected rule, quote and overtime separately', async () => {
  const { command, choose } = setup({ business_terms: businessTerms }, true)
  expect(screen.getByRole('button', { name: 'Save split-delivery rule' })).toBeDisabled()
  choose(oldQuote)
  for (const name of ['Save expedite quote', 'Delete this quote']) expect(screen.getByRole('button', { name })).toBeDisabled()
  choose(worker)
  for (const name of ['Record overtime availability', 'Remove this overtime window']) expect(screen.getByRole('button', { name })).toBeDisabled()
  expect(command).not.toHaveBeenCalled()
})

it('an invalid selection does not fall back to the first product, quote or worker', () => {
  const { choose, command } = setup({}, false, { kind: 'delivery-rule', productId: 'missing-product' })
  expect(screen.getByRole('button', { name: 'Save split-delivery rule' })).toBeDisabled()
  choose({ kind: 'quote', receiptId: 'RCPT-1', quoteId: 'missing-quote' })
  expect(screen.getByRole('alert')).toHaveTextContent('The selected quote no longer exists')
  expect(screen.queryByRole('button', { name: 'Save expedite quote' })).not.toBeInTheDocument()
  choose({ kind: 'overtime', targetType: 'worker', targetId: 'missing-worker' })
  expect(screen.getByRole('button', { name: 'Record overtime availability' })).toBeDisabled()
  expect(screen.getByText(/missing-worker/)).toBeVisible()
  expect(command).not.toHaveBeenCalled()
})
