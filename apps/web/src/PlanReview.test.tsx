import { render, screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { PlanReview } from './PlanReview'

it('shows changed order coverage, actual completion, deadlines and the worker overtime to approve', () => {
  render(<PlanReview zone="Asia/Singapore" review={{
    as_of: '2026-09-25T08:00:00+08:00', accept_before: '2026-09-25T08:15:00+08:00',
    orders: [{ order_id: 'SO-003', product_id: '6204', status: 'OPEN', quantity: 2000,
      due_at: '2026-09-29T17:30:00+08:00',
      qualified_completed_quantity: 0, in_progress_quantity: 50,
      previous_covered_quantity: 1200, plan_covered_quantity: 2000, uncovered_quantity: 0,
      previous_completion_at: null, planned_completion_at: '2026-09-29T16:00:00+08:00',
      planned_ready_today_quantity: 100, planned_on_time_quantity: 2000,
      direct_shortage_materials: [], material_quantity_upper_bound: 2000, forecast_requires_revalidation: false }],
    overtime: [{ worker_id: 'W-1', resource_id: 'M-1', start_at: '2026-09-25T18:00:00+08:00', end_at: '2026-09-25T19:00:00+08:00', minutes: 60 }],
  }} />)
  const row = screen.getByRole('row', { name: /SO-003/ })
  expect(within(row).getByText('2000 / 0 pcs')).toBeInTheDocument()
  expect(within(row).getByText('1200 → 2000 pcs')).toBeInTheDocument()
  expect(screen.getByText(/W-1 · M-1/)).toHaveTextContent('60 min')
  expect(screen.getByText(/Accept before/)).toBeInTheDocument()
})
