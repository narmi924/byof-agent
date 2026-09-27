import { describe, expect, it } from 'vitest'
import { legendOrder, operationStates, primaryLegend, shiftBands, toneOf } from './palette'

describe('schedule display meaning', () => {
  it('every operation state has a text label, basis and texture, not only a color', () => {
    for (const [state, style] of Object.entries(operationStates)) {
      expect(style.label, state).toMatch(/\S/)
      expect(style.basis, state).toMatch(/\S/)
      expect(['solid', 'stripe', 'dashed']).toContain(style.texture)
      expect(style.modifier, state).toMatch(/^is-/)
    }
  })

  it('the legend covers every state without duplicates', () => {
    expect(new Set(legendOrder).size).toBe(legendOrder.length)
    expect([...legendOrder].sort()).toEqual(Object.keys(operationStates).sort())
  })

  it('the permanent legend is a subset of the full legend', () => {
    expect(primaryLegend.every((state) => legendOrder.includes(state))).toBe(true)
  })

  it('each of the four shift shadings has an explanation', () => {
    expect(Object.keys(shiftBands)).toEqual(['NORMAL', 'OVERTIME', 'CLOSED', 'UNAVAILABLE'])
    for (const band of Object.values(shiftBands)) expect(band.basis).toMatch(/\S/)
  })

  it('unknown states are neutral and never guessed to be normal', () => {
    expect(toneOf('DOWN')).toBe('negative')
    expect(toneOf('CURRENT')).toBe('positive')
    expect(toneOf('STALE')).toBe('critical')
    expect(toneOf('SOMETHING_NEW')).toBe('neutral')
    expect(toneOf(undefined)).toBe('neutral')
  })
})
