import { expect, it } from 'vitest'
import { plainBusinessText } from './businessText'

it('turns internal IDs, status codes and UTC notes that actually appeared into business wording', () => {
  const text = plainBusinessText('The plan was approved by the manager and released (release aacdbc1a-aceb-4196-b8f5-22cd2a1cc9cf, source state ACTIVE), but the execution state is still NOT_STARTED; planned start is factory local 2026-09-26 08:30 (factory time 00:30 UTC). finish was rejected with EXECUTION_NOT_COMPLETED; wait until the execution source confirms IN_PROGRESS before closing this case.', 'Asia/Singapore')
  expect(text).not.toMatch(/[0-9a-f]{8}-|ACTIVE|NOT_STARTED|IN_PROGRESS|EXECUTION_NOT_COMPLETED|\bfinish\b|UTC|execution source/)
  expect(text).toContain('accepted by factory')
  expect(plainBusinessText('candidate 9875ddce generated; independent check PASS.')).toBe('plan generated; checked.')
  expect(plainBusinessText('Use production_exception to compare resupply')).toBe('Use disruption handling to compare resupply')
  expect(plainBusinessText('Solving failed with WIP_CONFIRMATION_REQUIRED')).toBe('Solving failed with remaining work to confirm')
})

it('converts month-day dates with Z or UTC written by the model into factory time', () => {
  expect(plainBusinessText('Receipt delayed to 09-27 09:00Z; reschedule needed', 'Asia/Singapore')).toBe('Receipt delayed to 09-27 17:00; reschedule needed')
  expect(plainBusinessText('2026-09-27 01:30 UTC start', 'Asia/Singapore')).toBe('09-27 09:30 start')
})
