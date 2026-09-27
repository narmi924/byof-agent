import type { EffectiveObjective, ObjectiveDefinition, PreferenceState } from '../preferenceContracts'

export const baselineDefinition: ObjectiveDefinition = { selection: 'delivery_first', objective_order: ['weighted_tardiness', 'incremental_overtime_metric', 'changed_operations', 'total_start_shift', 'makespan'], max_weighted_tardiness: null, max_incremental_overtime_minutes: null }
export const baselineEffective: EffectiveObjective = { status: 'READY', objective_version: 'delivery-v1', definition: baselineDefinition, sources: [], context_hash: null, reason: null }
export const baselineContracts = { 'delivery-v1': { definition: baselineDefinition, sources: [] } }
export function preferenceFixture(cases: PreferenceState['cases'] = []): PreferenceState {
  return { state_version: 0, effective: structuredClone(baselineEffective), heads: [], proposals: [], processes: [], cases, agent_proposals: [] }
}
