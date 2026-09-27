import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { BusinessStudyCard } from './BusinessStudyCard'
import { boardSnapshot, day } from './test/boardFixture'
import { businessJob, businessTerms } from './test/businessFixture'
import type { BusinessOption } from './businessContracts'

function open(option: Partial<BusinessOption> = {}, current = true) {
  render(<BusinessStudyCard job={{ ...businessJob(option), current }} snapshot={boardSnapshot()} />)
}

describe('read-only option comparison in the manager conversation', () => {
  it('a due date change must be confirmed explicitly before the option can be approved and executed by its ID', async () => {
    const onExecute = vi.fn()
    const job = businessJob({ kind: 'treatment', actions: [{ kind: 'order_due', target_id: 'SO-URGENT', action_id: 'due-action', quantity: 0, expected_version: 1, mode: 'standard', ready_at: `${day}T08:00:00Z` }], economics: {
      catalog_version: 'byof-demo-economics/1', evidence_mode: 'synthetic', currency: 'SGD', status: 'ESTIMATED',
      revenue_minor: 100000, variable_cost_minor: 70000, additional_cost_minor: 0, late_deduction_minor: 1000,
      net_contribution_minor: 29000, incremental_cash_outlay_minor: 0, improvement_minor: null, comparison_option_id: null,
      lines: [], assumptions: ['Test assumption'], missing: [],
    } })
    job.request = { ...job.request, kind: 'production_exception', order: null }
    render(<BusinessStudyCard job={job} snapshot={boardSnapshot()} onExecute={onExecute} />)
    const execute = screen.getByRole('button', { name: 'Approve and execute' })
    expect(execute).toBeDisabled()
    await userEvent.click(screen.getByRole('checkbox', { name: 'The customer agreed to the listed due date or quantity change' }))
    await userEvent.click(execute)
    expect(onExecute).toHaveBeenCalledWith(job.study!.options[0]!.option_id, false, true)
  })
  it('shows the impact on existing commitments and delivery times; the manager states the choice back in the conversation', async () => {
    open()
    expect(screen.getByText('200 / 200 pcs')).toBeInTheDocument()
    await userEvent.click(screen.getByText('Order impact, cost details and calculation conditions'))
    expect(screen.getByText('Impact on 1 existing order')).toBeVisible()
    expect(screen.getByText(/versus the previous plan 60 min later/)).toBeInTheDocument()
    expect(screen.getByText(/Advisory comparison only/)).toBeVisible()
    expect(screen.queryByText(/entered after verification by the site administrator/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('shows profit impact and cash outlay separately and claims no improvement without a baseline', async () => {
    open({ economics: {
      catalog_version: 'byof-demo-economics/1', evidence_mode: 'synthetic', currency: 'SGD', status: 'ESTIMATED',
      revenue_minor: 100000, variable_cost_minor: 70000, additional_cost_minor: 9000, late_deduction_minor: 0,
      net_contribution_minor: 21000, incremental_cash_outlay_minor: 9000, improvement_minor: null, comparison_option_id: null,
      lines: [], assumptions: ['Fixed price catalog.'], missing: [],
    } })
    expect(screen.getByText('Profit impact')).toBeVisible()
    expect(screen.getByText('New cash outlay')).toBeVisible()
    await userEvent.click(screen.getByText('Order impact, cost details and calculation conditions'))
    expect(screen.getByRole('region', { name: 'Costs and benefits' })).toHaveTextContent('SGD')
    expect(screen.getByText(/no feasible baseline, improvement not determined/)).toBeVisible()
  })

  it('shows the source quote and overtime separately without controls to confirm cost or overtime conditions', () => {
    open({ kind: 'receipt_expedite', quote_id: 'Q1', cost_minor: 12500, currency: 'CNY', allow_overtime: true, incremental_overtime_minutes: 60 })
    expect(screen.getByText('60 staff-min')).toBeInTheDocument()
    expect(screen.getByText(/CNY.*125\.00/, { selector: 'dd' })).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('an unknown cost is not free and an unknown delivery quantity is neither zero nor feasible', () => {
    open({ quote_id: 'Q1', status: 'UNKNOWN', on_time_quantity: null, completion_at: null, protects_existing_commitments: null })
    expect(screen.getByText('Cost not confirmed')).toBeInTheDocument()
    expect(screen.getByText('Pending / 200 pcs')).toBeInTheDocument()
    expect(screen.getByText('Not determined yet')).toBeInTheDocument()
    expect(screen.getByText(/The resupply cost is not confirmed yet/)).toBeInTheDocument()
    expect(screen.getByText('The impact on existing customer commitments is not confirmed yet.')).toBeInTheDocument()
  })

  it('outdated facts and diagnostic options that affect existing commitments ask to keep checking in this conversation', () => {
    open({ diagnostic_only: true, protects_existing_commitments: false }, false)
    expect(screen.getByText(/The shop floor facts have changed/)).toBeInTheDocument()
    expect(screen.getByText(/Ask the Agent in this conversation to check the latest facts and compare again/)).toBeInTheDocument()
    expect(screen.getByText('Diagnostic only')).toBeInTheDocument()
    expect(screen.getByText('This option affects existing customer commitments and needs further discussion.')).toBeInTheDocument()
  })

  it('a split-delivery option keeps the later delivery and does not claim a limited search is the maximum', () => {
    open({ kind: 'partial_delivery', on_time_quantity: 100, deliveries: [{ quantity: 100, ready_at: `${day}T08:00:00Z` }, { quantity: 100, ready_at: '2026-09-15T08:00:00Z' }] })
    expect(screen.getByText(/First delivery: 100 pcs/)).toBeInTheDocument()
    expect(screen.getByText(/Later delivery: 100 pcs/)).toBeInTheDocument()
    expect(screen.getByText('More may still be possible on time.')).toBeInTheDocument()
  })

  it('a found full delivery date does not claim to be the proven earliest', () => {
    open({ kind: 'earliest_completion' })
    expect(screen.getByText(/Full delivery: 200 pcs/)).toBeInTheDocument()
    expect(screen.getByText('This completion works; an earlier one may still be possible.')).toBeInTheDocument()
  })

  it('a reschedule proposal for an existing rush order is not described as protecting all existing commitments', () => {
    const job = businessJob({ kind: 'earliest_completion', on_time_quantity: 0 })
    job.request = { ...job.request, order: null, existing_order_id: 'SO-URGENT' }
    render(<BusinessStudyCard job={job} snapshot={boardSnapshot()} />)
    expect(screen.getByText(/Analyzing order SO-URGENT/)).toBeInTheDocument()
    expect(screen.getByText('Delivery commitments of other orders are protected; the due date proposal for the selected order is shown above.')).toBeInTheDocument()
    expect(screen.queryByText('Existing customer commitments are protected.')).not.toBeInTheDocument()
    expect(screen.getByText('0 / 200 pcs')).toBeInTheDocument()
  })

  it('when the original due date cannot be kept, it shows the proposed new due date and the change the customer must agree to instead of 0 pcs on time', () => {
    const job = businessJob({ kind: 'treatment', on_time_quantity: 0, actions: [{ kind: 'order_due', target_id: 'SO-001', action_id: 'due-action', quantity: 0, expected_version: 1, mode: 'standard', ready_at: `${day}T11:00:00Z` }] })
    job.request = { ...job.request, kind: 'production_exception', order: null, existing_order_id: 'SO-001' }
    render(<BusinessStudyCard job={job} snapshot={boardSnapshot()} />)
    expect(screen.getByText('New due date')).toBeInTheDocument()
    expect(screen.queryByText('0 / 200 pcs')).not.toBeInTheDocument()
    expect(screen.getByText(/^Customer agreement needed for new due dates: SO-001 from .+ to .+; other orders keep their original due dates\.$/)).toBeInTheDocument()
  })

  it('a failed calculation shows a readable reason and continues only through this conversation without calling the failure unsolvable', async () => {
    const onContinue = vi.fn()
    render(<BusinessStudyCard job={{ ...businessJob(), state: 'FAILED', study: null, error_code: 'WORKER_FAILURE' }} snapshot={boardSnapshot()} onContinue={onContinue} />)
    expect(screen.getByRole('alert')).toHaveTextContent('The calculation service could not finish this comparison')
    expect(screen.queryByText('Not feasible under current conditions')).not.toBeInTheDocument()
    await userEvent.click(screen.getByText('Why the comparison did not finish'))
    expect(screen.getByText('WORKER_FAILURE')).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Continue in this conversation' }))
    expect(onContinue).toHaveBeenCalledWith(expect.stringContaining('did not finish'))
  })

  it.each(['QUEUED', 'RUNNING'] as const)('shows the %s state and offers no duplicate start while calculating', state => {
    render(<BusinessStudyCard job={{ ...businessJob(), state, study: null }} snapshot={boardSnapshot()} onContinue={vi.fn()} />)
    expect(screen.getByRole('status')).toHaveTextContent(state === 'QUEUED' ? 'The comparison is queued' : 'Comparing feasibility')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('an outdated comparison can go on to check the latest facts; when disabled it cannot start conversation actions', async () => {
    const onContinue = vi.fn()
    const job = { ...businessJob(), current: false }
    const { rerender } = render(<BusinessStudyCard job={job} snapshot={boardSnapshot()} onContinue={onContinue} disabled />)
    expect(screen.getByRole('button', { name: 'Re-compare with latest facts' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Re-compare with latest facts' }))
    expect(onContinue).not.toHaveBeenCalled()
    rerender(<BusinessStudyCard job={job} snapshot={boardSnapshot()} onContinue={onContinue} />)
    await userEvent.click(screen.getByRole('button', { name: 'Re-compare with latest facts' }))
    expect(onContinue).toHaveBeenCalledWith(expect.stringContaining('Check the latest shop floor facts'))
  })

  it('explains a missing option without a split-delivery permission from the source, and does not report it as missing with one', () => {
    const snapshot = boardSnapshot(), job = businessJob()
    job.request = { ...job.request, order: null, existing_order_id: snapshot.orders[0]!.order_id }
    const { rerender } = render(<BusinessStudyCard job={job} snapshot={snapshot} />)
    expect(screen.getByText(/no split-delivery permission for this product/)).toBeVisible()
    const terms = { ...businessTerms, delivery_rules: [{ ...businessTerms.delivery_rules[0]!, product_id: snapshot.orders[0]!.product_id }] }
    rerender(<BusinessStudyCard job={job} snapshot={{ ...snapshot, business_terms: terms }} />)
    expect(screen.queryByText(/no split-delivery permission for this product/)).not.toBeInTheDocument()
  })

  it('explains a missing expedite without a valid source quote and does not treat an unknown price as no quote', () => {
    const snapshot = boardSnapshot(), job = businessJob({ kind: 'shared_material' })
    job.request = { ...job.request, kind: 'material_shortage', order: null, receipt_id: 'R1' }
    const { rerender } = render(<BusinessStudyCard job={job} snapshot={snapshot} />)
    expect(screen.getByText(/no valid expedited-receipt quote/)).toBeVisible()
    const quote = { ...businessTerms.expedite_quotes[0]!, receipt_id: 'R1', valid_until: `${day}T12:00:00Z`, cost_minor: null, currency: null }
    const withQuote = { ...snapshot, business_terms: { ...businessTerms, expedite_quotes: [quote] }, receipts: [{ receipt_id: 'R1', material_id: 'IR', quantity: quote.quantity, unit: 'EA', eta: quote.original_eta, status: 'CONFIRMED', received_at: null, version: quote.receipt_version }] }
    rerender(<BusinessStudyCard job={job} snapshot={withQuote} />)
    expect(screen.queryByText(/no valid expedited-receipt quote/)).not.toBeInTheDocument()
    rerender(<BusinessStudyCard job={job} snapshot={{ ...withQuote, business_terms: { ...businessTerms, expedite_quotes: [{ ...quote, valid_until: snapshot.snapshot_clock }] } }} />)
    expect(screen.getByText(/no valid expedited-receipt quote/)).toBeVisible()
  })
})

describe('approved response options', () => {
  it('marks the approved option and no longer offers approve, comment or re-compare', () => {
    const job = businessJob({ economics: {
      catalog_version: 'byof-demo-economics/1', evidence_mode: 'synthetic', currency: 'SGD', status: 'ESTIMATED',
      revenue_minor: 100000, variable_cost_minor: 70000, additional_cost_minor: 0, late_deduction_minor: 0,
      net_contribution_minor: 30000, incremental_cash_outlay_minor: 0, improvement_minor: null, comparison_option_id: null,
      lines: [], assumptions: [], missing: [],
    } })
    job.request = { ...job.request, kind: 'production_exception', order: null }
    render(<BusinessStudyCard job={{ ...job, current: false }} snapshot={boardSnapshot()} approvedOptionId={job.study!.options[0]!.option_id} onExecute={vi.fn()} onDiscuss={vi.fn()} onContinue={vi.fn()} />)
    expect(screen.getByRole('status')).toHaveTextContent('Approved “Regular-shift fulfilment”')
    expect(screen.getByText('Approved')).toBeVisible()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByText(/The shop floor facts have changed/)).not.toBeInTheDocument()
  })
})
