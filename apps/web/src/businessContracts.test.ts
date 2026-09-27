import { describe, expect, it } from 'vitest'
import { parseAssistantState } from './assistantContracts'
import { isBusinessTerms, isBusinessStudyRequest, parseBusinessStudyJob, quotedCost } from './businessContracts'
import { businessJob, businessTerms } from './test/businessFixture'

describe('read/write contract of option comparisons', () => {
  it('old assistant responses accept an empty study list; new results keep their non-release meaning', () => {
    const state = { material_balance: { snapshot_id: null, shortfalls: [] }, actions: [], learning: { weights: { delivery: 0.5, stability: 0.3, overtime: 0.2 }, samples: 0, active: false, explicit: null, summary: '' } }
    expect(parseAssistantState(state).business_studies).toEqual([])
    const job = businessJob()
    expect(parseAssistantState({ ...state, business_studies: [job] }).business_studies).toEqual([job])
    expect(() => parseBusinessStudyJob({ ...job, advisory_only: false })).toThrow('not recognized')
    expect(() => parseBusinessStudyJob({ ...job, study: { ...job.study, publishable: true } })).toThrow('not recognized')
  })
  it('keeps unknown numbers as null and rejects invalid costs or options claiming to be releasable', () => {
    expect(parseBusinessStudyJob(businessJob({ status: 'UNKNOWN', on_time_quantity: null, completion_at: null })).study?.options[0]?.on_time_quantity).toBeNull()
    expect(() => parseBusinessStudyJob(businessJob({ cost_minor: -1, currency: 'CNY' }))).toThrow('not recognized')
    const job = businessJob()
    expect(() => parseBusinessStudyJob({ ...job, study: { ...job.study, options: [{ ...job.study!.options[0], publishable: true }] } })).toThrow('not recognized')
    expect(isBusinessTerms(businessTerms)).toBe(true)
    expect(isBusinessTerms({ ...businessTerms, expedite_quotes: [{ ...businessTerms.expedite_quotes[0], cost_minor: null }] })).toBe(false)
  })
  it('new comparisons reference a synced order and reject both order facts and an order reference', () => {
    const request = { ...businessJob().request, order: null, existing_order_id: 'SO-001' }
    expect(isBusinessStudyRequest(request)).toBe(true)
    expect(isBusinessStudyRequest({ ...request, order: businessJob().request.order })).toBe(false)
    expect(isBusinessStudyRequest({ ...request, existing_order_id: null })).toBe(false)
    expect(isBusinessStudyRequest({ ...request, kind: 'material_shortage' })).toBe(false)
    const job = businessJob()
    expect(parseBusinessStudyJob({ ...job, request, study: { ...job.study, request } }).request.existing_order_id).toBe('SO-001')
  })
  it('a linked case must be a valid ID; missing or null historical results stay unlinked', () => {
    const job = businessJob()
    expect(parseBusinessStudyJob(job).case_id).toBe('case-1')
    expect(parseBusinessStudyJob({ ...job, case_id: undefined }).case_id).toBeNull()
    expect(parseBusinessStudyJob({ ...job, case_id: null }).case_id).toBeNull()
    expect(() => parseBusinessStudyJob({ ...job, case_id: 42 })).toThrow('not recognized')
    expect(() => parseBusinessStudyJob({ ...job, case_id: '' })).toThrow('not recognized')
  })
  it('distinguishes zero and unknown costs and converts minor units per currency', () => {
    expect(quotedCost(null, null)).toBe('Cost not confirmed')
    expect(quotedCost(0, 'CNY')).toMatch(/0\.00/)
    expect(quotedCost(500, 'JPY')).toMatch(/500/)
    expect(quotedCost(12500, 'CNY')).toMatch(/125\.00/)
  })
})
