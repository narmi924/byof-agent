import { describe, expect, it } from 'vitest'
import { parseEffectiveObjective, parseObjectiveDefinition, parsePreferenceState } from './preferenceContracts'
import type { ObjectiveDefinition, PreferenceHead, PreferenceSource } from './preferenceContracts'
import { baselineDefinition, baselineEffective, preferenceFixture } from './test/preferenceFixture'

const factoryId = 'factory-preferences'
const now = '2026-09-17T08:00:00Z'
const stable: ObjectiveDefinition = { selection: 'stability_first', objective_order: ['changed_operations', 'total_start_shift', 'weighted_tardiness', 'incremental_overtime_metric', 'makespan'], max_weighted_tardiness: 120, max_incremental_overtime_minutes: -30 }
const cases = [{ case_id: 'case-one', title: 'Schedule after repair', owner_id: 'planner-one', state: 'WAITING' }, { case_id: 'case-two', title: 'Rush order coordination', owner_id: 'planner-two', state: 'PLANNING' }]
function source(scope_type: PreferenceSource['scope_type'] = 'FACTORY', scope_id = factoryId): PreferenceSource {
  return { preference_id: 'preference-one', version: 1, scope_type, scope_id, confirmed_by: 'planner-one', confirmed_at: now, clock: 'real', product_id: scope_type === 'PROCESS' ? 'product-one' : null, route_version: scope_type === 'PROCESS' ? 'route-two' : null }
}

describe('rejected cases of the objective data contract', () => {
  it('rejects duplicate or unknown objectives, decimals and missing required bounds; accepts a negative incremental bound', () => {
    expect(parseObjectiveDefinition(stable)).toEqual(stable)
    for (const invalid of [{ ...stable, objective_order: ['makespan', 'makespan', 'changed_operations', 'total_start_shift', 'weighted_tardiness'] }, { ...stable, max_weighted_tardiness: -1 }, { ...stable, max_incremental_overtime_minutes: 0.5 }, { ...stable, max_incremental_overtime_minutes: null }, { ...stable, max_weighted_tardiness: null }]) expect(() => parseObjectiveDefinition(invalid)).toThrow()
  })
  it('real confirmation sources need a time zone, route version and real clock; rejects a cross-factory default head', () => {
    const initial = preferenceFixture(cases)
    initial.effective = { ...baselineEffective, sources: [source('PROCESS', 'product-one:route-two')] }
    expect(parsePreferenceState(initial, factoryId)).toEqual(initial)
    expect(() => parseEffectiveObjective({ ...initial.effective, sources: [{ ...source(), confirmed_at: '2026-09-17T08:00:00' }] })).toThrow()
    expect(() => parseEffectiveObjective({ ...initial.effective, sources: [{ ...source('PROCESS'), route_version: null }] })).toThrow()
    expect(() => parseEffectiveObjective({ ...initial.effective, sources: [{ ...source(), clock: 'business' }] })).toThrow()
    initial.heads = [{ scope_type: 'FACTORY', scope_id: 'other-factory', preference_id: 'x', active: true, version: 1, definition: baselineDefinition } satisfies PreferenceHead]
    expect(() => parsePreferenceState(initial, factoryId)).toThrow('The preference scope or version does not match')
  })
})
