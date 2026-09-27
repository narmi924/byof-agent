import { describe, expect, it } from 'vitest'
import { readViewState, viewSearch, viewUrl } from './viewState'

describe('view state and links', () => {
  it('opens the Agent conversation by default; the two Agent pages use their own paths', () => {
    expect(readViewState('')).toMatchObject({ view: 'chat', group: 'resource', day: '', error: '' })
    expect(viewUrl(readViewState(''))).toBe('/agent/chat')
    expect(readViewState('?factory_id=skf-workshop', '/agent/timeline').view).toBe('board')
    expect(readViewState('?view=plan', '/agent/chat').view).toBe('chat')
    expect(readViewState('?view=board', '/factory/facts').view).toBe('facts')
  })

  it('task links from email land directly in the case view where the reply entry is', () => {
    const state = readViewState('?factory_id=skf-workshop&case_id=case-1&task_id=task-1')
    expect(state).toMatchObject({ view: 'cases', factoryId: 'skf-workshop', caseId: 'case-1', taskId: 'task-1', error: '' })
  })

  it('links with only a case land in the case view', () => {
    expect(readViewState('?factory_id=skf-workshop&case_id=case-1').view).toBe('cases')
  })

  it('links missing the parent ID keep their explanation', () => {
    expect(readViewState('?task_id=task-1').error).toBe('The case link is missing the factory or case; check the original link.')
  })

  it('unknown views or groupings fall back to the defaults instead of failing', () => {
    expect(readViewState('?view=nowhere&group=machine')).toMatchObject({ view: 'chat', group: 'resource' })
  })

  it('invalid date parameters are ignored', () => {
    expect(readViewState('?day=2026-02-30').day).toBe('')
    expect(readViewState('?day=2026-09-14').day).toBe('2026-09-14')
  })

  it('repeated parameters count as missing so crafted links cannot change the display', () => {
    expect(readViewState('?operation=a&operation=b').operationId).toBe('')
  })

  it('written links read back to the same state', () => {
    const state = readViewState('?factory_id=skf-workshop&view=board&day=2026-09-15&group=order&operation=SO-001-R001-B001-OP10')
    const url = viewUrl(state)
    expect(url).toContain('/agent/timeline?')
    expect(url).toContain('group=order')
    expect(readViewState(new URL(url, 'http://localhost').search, '/agent/timeline')).toMatchObject(state)
  })

  it('only writes parameters that differ from the defaults', () => {
    expect(viewSearch(readViewState('?factory_id=f1'))).toBe('?factory_id=f1')
    expect(viewUrl({ ...readViewState(''), view: 'plan', candidateId: 'cand-1' })).toBe('/factory/plan?candidate=cand-1')
  })

  it('the factory page no longer writes business groups into the address', () => {
    const orders = readViewState('?factory_id=skf-workshop&module=orders', '/factory/facts')
    expect(viewUrl(orders)).toBe('/factory/facts?factory_id=skf-workshop')
    expect(viewUrl({ ...orders, view: 'chat' })).toBe('/agent/chat?factory_id=skf-workshop')
  })

  it('does not write case links without a factory ID, to avoid unparseable addresses', () => {
    expect(viewSearch({ ...readViewState(''), caseId: 'case-1', taskId: 'task-1' })).toBe('')
  })

  it('old task links open the factory case page; preview plans stay in the timeline address', () => {
    const task = readViewState('?factory_id=skf-workshop&case_id=case-1&task_id=task-1')
    expect(viewUrl(task)).toBe('/factory/cases?factory_id=skf-workshop&case_id=case-1&task_id=task-1')
    const preview = { ...readViewState('?factory_id=skf-workshop', '/agent/chat'), view: 'board' as const, candidateId: 'candidate-1' }
    expect(viewUrl(preview)).toBe('/agent/timeline?factory_id=skf-workshop&candidate=candidate-1')
  })
})
