import { render, screen, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { ExecutionRecords } from './ExecutionRecords'
import * as api from './api'
import type { Publication } from './contracts'
import { boardSnapshot } from './test/boardFixture'

vi.mock('./api', async importOriginal => ({ ...await importOriginal<typeof import('./api')>(), readAssistant: vi.fn() }))
const snapshot = boardSnapshot()
const learning = { weights: { delivery: 0.34, stability: 0.33, overtime: 0.33 }, samples: 0, active: false, explicit: null, summary: '' }

beforeEach(() => {
  vi.mocked(api.readAssistant).mockResolvedValue({ learning, material_balance: { snapshot_id: null, shortfalls: [] }, actions: [
    { action_id: 'exec', request_id: 'r1', run_id: snapshot.run_id!, kind: 'treatment_execute', state: 'DONE', created_at: '2026-09-14T02:00:00Z',
      payload: { option: { title: 'Expedited resupply and required resource measures', actions: [{ kind: 'supply', target_id: 'IR-6204', quantity: 300 }], economics: { status: 'ESTIMATED', incremental_cash_outlay_minor: 372100 } } },
      result: { stage: 'PUBLISHING', summary: 'Measures applied; the schedule was released.', release_id: 'rel-1' } },
    { action_id: 'plan', request_id: 'r2', run_id: snapshot.run_id!, kind: 'approve', state: 'QUEUED', created_at: '2026-09-14T01:00:00Z', payload: {}, result: null },
    { action_id: 'pref', request_id: 'r3', run_id: snapshot.run_id!, kind: 'preference', state: 'DONE', created_at: '2026-09-14T03:00:00Z', payload: {}, result: null },
    { action_id: 'old-run', request_id: 'r4', run_id: 'previous-run', kind: 'approve', state: 'DONE', created_at: '2026-09-13T03:00:00Z', payload: {}, result: null },
  ] } as never)
})

it('records each manager approval with its measures, costs and factory receipt', async () => {
  const publication = { candidate_id: 'c1', error_code: null, release: { release_id: 'rel-1', source_state: 'ACTIVE', execution_state: 'IN_PROGRESS' } } as unknown as Publication
  render(<ExecutionRecords factoryId={snapshot.factory_id} snapshot={snapshot} publications={[publication]} />)
  const items = await screen.findAllByRole('listitem')
  const records = items.filter(item => item.classList.contains('record-item'))
  expect(records).toHaveLength(2)
  const executed = records[0]!
  expect(within(executed).getByRole('heading', { name: 'Expedited resupply and required resource measures' })).toBeVisible()
  expect(executed).toHaveTextContent('Resupply · IR-6204 · 300')
  expect(executed).toHaveTextContent('SGD')
  expect(executed).not.toHaveTextContent(/simulat/i)
  expect(executed).toHaveTextContent('Accepted by factory')
  expect(executed).toHaveTextContent('In production')
  expect(records[1]).toHaveTextContent('Schedule plan')
  expect(records[1]).toHaveTextContent('Not released yet')
  expect(screen.queryByText(/Recommendation preference/)).not.toBeInTheDocument()
})
