import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { journalKey, usePendingSlot, writePending } from './pendingJournal'
import type { PlanningAttempt } from './pendingJournal'
import { businessJob } from './test/businessFixture'

const original: PlanningAttempt = { kind: 'solve', requestId: 'request-original', allowOvertime: true, earliest: '2026-09-19T08:15:00Z' }
const account = 'planner-one', factory = 'factory-one'
const key = journalKey(account, factory)
const oldAcceptance = { request_id: 'retired-accept', run_id: 'run-1', payload: { job_id: 'job-1', option_id: 'option-1', study_hash: 'a'.repeat(64), confirm_extra_cost: true, allow_overtime: true } }
const oldStudy = { kind: 'study', caseId: null, taskId: null, body: { request_id: 'retired-study', run_id: 'run-1', expected_snapshot_hash: 'a'.repeat(64), request: businessJob().request } }
const currentChat = { kind: 'chat' as const, caseId: 'case-1', taskId: null, body: { request_id: 'pending-chat', message: 'Keep the original chat request' } }
const currentApproval: PlanningAttempt = { kind: 'approval', requestId: 'pending-approval', candidateId: 'candidate-1', candidateHash: 'a'.repeat(64), scope: 'publish_plan', decision: 'APPROVED', mode: 'STRICT' }
const archivedKeys = () => Object.keys(sessionStorage).filter(item => item.startsWith(`${key}:retired-business:`))
const savedRecord = (slots: Record<string, unknown>) => JSON.stringify({ version: 1, userId: account, factoryId: factory, slots })

describe('pending recovery of administrator source-conditioned commands', () => {
  const quote = { expected_terms_version: null, quote_id: 'Q1', receipt_id: 'R1', expected_receipt_version: 2, expedited_eta: '2026-09-14T05:00:00Z', valid_until: '2026-09-14T04:00:00Z', source_reference: 'supplier-ref', cost_minor: null, currency: null }
  const overtime = { target_type: 'worker', target_id: 'W01', expected_version: 3, action: 'add', start_at: '2026-09-15T09:30:00Z', end_at: '2026-09-15T11:30:00Z' }
  const attempt = (kind: string, payload: unknown) => ({ label: 'Record source facts', command: { request_id: 'source-request', run_id: 'run-1', kind, payload } })
  it.each([
    ['delivery_rule.set', { expected_terms_version: 'terms-2', product_id: 'SKU1', partial_delivery_allowed: true, minimum_partial_quantity: 50, max_deliveries: 2 }],
    ['expedite_quote.set', quote],
    ['expedite_quote.set', { ...quote, expected_terms_version: 'terms-2', cost_minor: 0, currency: 'CNY' }],
    ['expedite_quote.remove', { expected_terms_version: 'terms-2', quote_id: 'Q1' }],
    ['overtime_window.set', overtime],
    ['overtime_window.set', { ...overtime, target_type: 'resource', action: 'remove' }],
  ])('%s keeps the original version, empty quote and full payload without creating a new request', (kind, payload) => {
    const original = attempt(kind as string, payload)
    writePending(account, factory, 'simulator', original)
    const view = renderHook(() => usePendingSlot('simulator', account, factory))
    expect(view.result.current.pending).toEqual(original)
    expect(view.result.current.error).toBe('')
  })
  it.each([
    ['expedite_quote.set', { ...quote, currency: 'CNY' }],
    ['expedite_quote.set', { ...quote, cost_minor: -1, currency: 'CNY' }],
    ['expedite_quote.set', { ...quote, cost_minor: 1.5, currency: 'CNY' }],
    ['expedite_quote.set', { ...quote, cost_minor: 10, currency: 'JPY' }],
    ['expedite_quote.set', { ...quote, expected_receipt_version: 0 }],
    ['expedite_quote.set', { ...quote, cost_minor: 0, currency: 'CNY', confirm_extra_cost: true }],
    ['expedite_quote.remove', { quote_id: 'Q1' }],
    ['delivery_rule.set', { expected_terms_version: null, product_id: 'SKU1', partial_delivery_allowed: true, minimum_partial_quantity: 50, max_deliveries: 1 }],
    ['overtime_window.set', { ...overtime, expected_version: undefined }],
    ['overtime_window.set', { ...overtime, start_at: '2026-09-15T09:30:01Z' }],
    ['overtime_window.set', { ...overtime, end_at: overtime.start_at }],
  ])('%s with damaged or unauthorized fields is not resent and does not overwrite the original record %#', (kind, payload) => {
    const raw = savedRecord({ simulator: attempt(kind as string, payload) })
    sessionStorage.setItem(key, raw)
    const view = renderHook(() => usePendingSlot('simulator', account, factory))
    expect(view.result.current.pending).toBeNull()
    expect(view.result.current.error).toContain('damaged or unsupported')
    expect(sessionStorage.getItem(key)).toBe(raw)
  })
})

describe('session recovery record of pending actions', () => {
  it.each([
    { business: oldAcceptance, assistant: currentChat },
    { assistant: oldStudy },
    { assistant: { kind: 'action', caseId: null, taskId: null, body: { ...oldAcceptance, kind: 'business_accept' } } },
  ])('retired business requests are archived in full while valid approvals and chats stay as they are %#', obsolete => {
    const network = vi.fn(); vi.stubGlobal('fetch', network)
    const raw = savedRecord({ ...obsolete, planning: currentApproval })
    sessionStorage.setItem(key, raw)
    const view = renderHook(() => usePendingSlot('assistant', account, factory))
    expect(view.result.current.canArchiveRetiredBusiness).toBe(true)
    act(() => { expect(view.result.current.archiveRetiredBusiness()).toBe(true) })
    expect(archivedKeys()).toHaveLength(1)
    expect(sessionStorage.getItem(archivedKeys()[0]!)).toBe(raw)
    expect(JSON.parse(sessionStorage.getItem(key)!).slots).toEqual({ planning: currentApproval, ...('business' in obsolete ? { assistant: currentChat } : {}) })
    expect(view.result.current.pending).toEqual('business' in obsolete ? currentChat : null)
    expect(view.result.current.error).toBe('')
    expect(view.result.current.canArchiveRetiredBusiness).toBe(false)
    expect(network).not.toHaveBeenCalled()
  })

  it.each(['archive', 'journal', 'silent-archive', 'silent-journal'])('keeps the block and the original record when writing the archive or current record fails: %s', failure => {
    const raw = savedRecord({ business: oldAcceptance, planning: currentApproval })
    sessionStorage.setItem(key, raw)
    const view = renderHook(() => usePendingSlot('assistant', account, factory))
    const store = Storage.prototype.setItem
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, storageKey, value) {
      const fail = failure.endsWith('archive') ? storageKey.startsWith(`${key}:retired-business:`) : storageKey === key
      if (fail) {
        if (failure.startsWith('silent')) return
        throw new DOMException('Full', 'QuotaExceededError')
      }
      store.call(this, storageKey, value)
    })
    act(() => { expect(view.result.current.archiveRetiredBusiness()).toBe(false) })
    expect(view.result.current.error).toContain('Recovering the old record did not finish')
    expect(view.result.current.canArchiveRetiredBusiness).toBe(true)
    expect(sessionStorage.getItem(key)).toBe(raw)
    for (const archive of archivedKeys()) expect(sessionStorage.getItem(archive)).toBe(raw)
    act(() => { expect(view.result.current.save(currentChat)).toBe(false) })
    expect(sessionStorage.getItem(key)).toBe(raw)
  })

  it.each([
    '{broken-json',
    savedRecord({ business: { ...oldAcceptance, payload: { ...oldAcceptance.payload, study_hash: 'invalid' } } }),
    savedRecord({ business: oldAcceptance, planning: { ...currentApproval, decision: 'UNKNOWN' } }),
  ])('unknown damaged records offer no archive recovery and cannot be cleared along the way %#', raw => {
    sessionStorage.setItem(key, raw)
    const view = renderHook(() => usePendingSlot('assistant', account, factory))
    expect(view.result.current.canArchiveRetiredBusiness).toBe(false)
    act(() => { expect(view.result.current.archiveRetiredBusiness()).toBe(false) })
    expect(sessionStorage.getItem(key)).toBe(raw)
    expect(archivedKeys()).toHaveLength(0)
  })

  it('does not overwrite a newly written pending request when the record changed before the click', () => {
    sessionStorage.setItem(key, savedRecord({ business: oldAcceptance }))
    const view = renderHook(() => usePendingSlot('assistant', account, factory))
    const updated = savedRecord({ business: oldAcceptance, planning: currentApproval })
    sessionStorage.setItem(key, updated)
    act(() => { expect(view.result.current.archiveRetiredBusiness()).toBe(false) })
    expect(sessionStorage.getItem(key)).toBe(updated)
    expect(archivedKeys()).toHaveLength(0)
  })

  it('remounting and switching factory keep the original input; reading sends no network request', () => {
    const network = vi.fn(); vi.stubGlobal('fetch', network)
    const first = renderHook(() => usePendingSlot('planning', account, factory))
    act(() => { expect(first.result.current.save(original)).toBe(true) })
    first.unmount()
    const next = renderHook(({ factoryId }) => usePendingSlot('planning', account, factoryId), { initialProps: { factoryId: factory } })
    expect(next.result.current.pending).toEqual(original)
    next.rerender({ factoryId: 'factory-two' }); expect(next.result.current.pending).toBeNull()
    next.rerender({ factoryId: factory }); expect(next.result.current.pending).toEqual(original)
    expect(network).not.toHaveBeenCalled()
  })

  it('a missing identity or another account cannot read or overwrite the original account action; the original account still needs to check', () => {
    writePending(account, factory, 'planning', original)
    const view = renderHook(({ userId }: { userId: string | undefined }) => usePendingSlot('planning', userId, factory), { initialProps: { userId: undefined as string | undefined } })
    expect(view.result.current.pending).toBeNull()
    act(() => { expect(view.result.current.save(original)).toBe(false) })
    expect(view.result.current.error).toContain('account ID is not confirmed')
    view.rerender({ userId: 'planner-two' }); expect(view.result.current.pending).toBeNull()
    act(() => { expect(view.result.current.save({ ...original, requestId: 'second-account' })).toBe(true) })
    view.rerender({ userId: account }); expect(view.result.current.pending).toEqual(original)
    expect(JSON.parse(sessionStorage.getItem(key)!).slots.planning).toEqual(original)
  })

  it('an unknown original request cannot change its ID or payload, and a new failure does not overwrite the old record', () => {
    writePending(account, factory, 'planning', original)
    const saved = sessionStorage.getItem(key)
    for (const replacement of [{ ...original, requestId: 'replacement' }, { ...original, allowOvertime: false }, { ...original, earliest: '2026-09-19T09:00:00Z' }]) {
      expect(() => writePending(account, factory, 'planning', replacement)).toThrow('check the original request')
      expect(sessionStorage.getItem(key)).toBe(saved)
    }
    writePending(account, factory, 'planning', { earliest: original.earliest, allowOvertime: true, requestId: original.requestId, kind: 'solve' })
    expect(JSON.parse(sessionStorage.getItem(key)!).slots.planning).toEqual(original)
  })

  it.each([
    '{broken-json',
    JSON.stringify({ version: 2, userId: account, factoryId: factory, slots: {} }),
    JSON.stringify({ version: 1, userId: 'other-account', factoryId: factory, slots: { planning: original } }),
    JSON.stringify({ version: 1, userId: account, factoryId: 'other-factory', slots: { planning: original } }),
    JSON.stringify({ version: 1, userId: account, factoryId: factory, slots: { planning: { ...original, csrf_token: 'never-save' } } }),
    JSON.stringify({ version: 1, userId: account, factoryId: factory, slots: { planning: { ...original, earliest: 'tomorrow' } } }),
    JSON.stringify({ version: 1, userId: account, factoryId: factory, slots: { simulator: { label: 'Unsupported action', command: { request_id: 'request-one', run_id: 'run-one', kind: 'shell', payload: {} } } } }),
    JSON.stringify({ version: 1, userId: account, factoryId: factory, slots: { cases: { kind: 'create', caseId: null, taskId: null, success: 'Save', body: { request_id: 'request-one', message: 'Case', start_new: true, confirmed: true } } } }),
    'x'.repeat(65_537),
  ])('polluted or unsupported records are kept as they are and block new requests #%#', (raw) => {
    sessionStorage.setItem(key, raw)
    const view = renderHook(() => usePendingSlot('planning', account, factory))
    expect(view.result.current.pending).toBeNull(); expect(view.result.current.error).toContain('damaged or unsupported')
    act(() => { expect(view.result.current.save(original)).toBe(false) })
    expect(sessionStorage.getItem(key)).toBe(raw)
  })

  it.each(['getItem', 'setItem'] as const)('explicitly blocks submission when storage %s fails', (method) => {
    vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new DOMException('Storage denied', 'SecurityError') })
    const view = renderHook(() => usePendingSlot('planning', account, factory))
    act(() => { expect(view.result.current.save(original)).toBe(false) })
    expect(view.result.current.error).toContain('the request has not been sent')
  })

  it('a silently lost storage write also blocks sending; a failed delete after checking keeps the original request', () => {
    const view = renderHook(() => usePendingSlot('planning', account, factory))
    const failed = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {})
    act(() => { expect(view.result.current.save(original)).toBe(false) })
    failed.mockRestore()
    act(() => { expect(view.result.current.save(original)).toBe(true) })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Quota') })
    act(() => { expect(view.result.current.clear(original)).toBe(false) })
    expect(view.result.current.pending).toEqual(original)
    expect(view.result.current.error).toContain('the local record was not cleared')
  })

  it('a record polluted after recovery cannot use the old in-memory value to send or clear', () => {
    writePending(account, factory, 'planning', original)
    const view = renderHook(() => usePendingSlot('planning', account, factory))
    expect(view.result.current.pending).toEqual(original)
    sessionStorage.setItem(key, 'corrupted-after-read')
    act(() => { expect(view.result.current.save(original)).toBe(false); expect(view.result.current.clear(original)).toBe(false) })
    expect(sessionStorage.getItem(key)).toBe('corrupted-after-read')
  })
})
