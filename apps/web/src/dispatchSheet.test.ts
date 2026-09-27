import { expect, it } from 'vitest'
import { dispatchCsv } from './dispatchSheet'
import type { OperationIdentity } from './scheduleModel'

it('the dispatch sheet lists only the day\'s operations, sorted by machine and start, in factory time', () => {
  const identity = { operationId: 'B1-OP10', orderId: 'SO-1', batchId: 'B1', operationCode: 'OP10', stepName: 'Assembly, check', quantity: 50 } as OperationIdentity
  const index = new Map([['B1-OP10', identity]])
  const at = (h: number) => new Date(Date.UTC(2026, 8, 26, h)).toISOString()
  const csv = dispatchCsv([
    { operation_id: 'B1-OP10', resource_id: 'KIT-02', worker_id: 'W1', changeover_start: at(2), start_at: at(2), end_at: at(3) },
    { operation_id: 'B1-OP10', resource_id: 'KIT-01', worker_id: 'W2', changeover_start: at(1), start_at: at(1), end_at: at(2) },
    { operation_id: 'X', resource_id: 'KIT-01', worker_id: 'W2', changeover_start: at(20), start_at: at(20), end_at: at(21) },
  ], index, Date.UTC(2026, 8, 26, 0), Date.UTC(2026, 8, 26, 12), 'Asia/Singapore')
  const lines = csv.replace('\uFEFF', '').trim().split('\r\n')
  expect(lines).toHaveLength(3)
  expect(lines[1]).toBe('KIT-01,09:00,10:00,W2,SO-1,B1,"OP10 Assembly, check",50')
  expect(lines[2]!.startsWith('KIT-02,10:00,11:00')).toBe(true)
})
