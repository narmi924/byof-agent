export const objectiveNames = ['weighted_tardiness', 'incremental_overtime_metric', 'changed_operations', 'total_start_shift', 'makespan'] as const
export type ObjectiveName = typeof objectiveNames[number]
export type PreferenceSelection = 'delivery_first' | 'stability_first' | 'overtime_first' | 'custom'
export type PreferenceScope = 'FACTORY' | 'PROCESS' | 'CASE'
export const objectiveLabels: Record<ObjectiveName, string> = { weighted_tardiness: 'Weighted tardiness', incremental_overtime_metric: 'Added overtime', changed_operations: 'Changed operations', total_start_shift: 'Total start shift', makespan: 'Makespan' }
export const preferenceLabels: Record<PreferenceSelection, string> = { delivery_first: 'Delivery first', stability_first: 'Plan stability first', overtime_first: 'Less overtime first', custom: 'Custom objective order' }
export const presetOrders: Record<Exclude<PreferenceSelection, 'custom'>, ObjectiveName[]> = {
  delivery_first: [...objectiveNames],
  stability_first: ['changed_operations', 'total_start_shift', 'weighted_tardiness', 'incremental_overtime_metric', 'makespan'],
  overtime_first: ['incremental_overtime_metric', 'weighted_tardiness', 'changed_operations', 'total_start_shift', 'makespan'],
}
export interface ObjectiveDefinition { selection: PreferenceSelection; objective_order: ObjectiveName[]; max_weighted_tardiness: number | null; max_incremental_overtime_minutes: number | null }
export interface PreferenceSource { preference_id: string; version: number; scope_type: PreferenceScope; scope_id: string; confirmed_by: string; confirmed_at: string; clock: 'real'; product_id: string | null; route_version: string | null }
export interface PreferenceCoordination { coordination_id: string; confirmed_by: string; confirmed_at: string; reason: string; context_hash: string }
interface ResolutionEvidence { coordination?: PreferenceCoordination | null; resolution_version?: number }
export interface ObjectiveContract extends ResolutionEvidence { definition: ObjectiveDefinition; sources: PreferenceSource[] }
export interface EffectiveObjective extends ResolutionEvidence { status: 'READY' | 'CONFLICT' | 'INVALID'; objective_version: string | null; definition: ObjectiveDefinition | null; sources: PreferenceSource[]; context_hash: string | null; reason: string | null }
export interface PreferenceHead { scope_type: PreferenceScope; scope_id: string; version: number; active: boolean; preference_id: string; definition: ObjectiveDefinition }
export interface PreferenceProposal { proposal_id: string; state: 'PENDING' | 'CONFIRMED' | 'REJECTED'; scope_type: PreferenceScope; scope_id: string; definition: ObjectiveDefinition; expected_version: number; created_at: string; proposer_id: string; reason: string }
export interface PreferenceState { state_version: number; effective: EffectiveObjective; heads: PreferenceHead[]; proposals: PreferenceProposal[]; processes: { scope_id: string; product_id: string; name: string; route_version: string }[]; cases: { case_id: string; title: string; owner_id: string; state: string }[]; agent_proposals: { proposal_id: string; case_id: string; selection: PreferenceSelection; reason: string; missing_fields: string[] }[] }
export interface ProposalInput { request_id: string; scope_type: PreferenceScope; scope_id: string; definition: ObjectiveDefinition; expected_version: number; reason: string; source_proposal_id?: string }
const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const string = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value)
const nonnegative = (value: unknown): value is number => integer(value) && value >= 0
const scope = (value: unknown) => ['FACTORY', 'PROCESS', 'CASE'].includes(String(value))
const selection = (value: unknown) => ['delivery_first', 'stability_first', 'overtime_first', 'custom'].includes(String(value))
const timestamp = (value: unknown) => string(value) && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))
const source = (value: unknown) => object(value) && ['preference_id', 'scope_id', 'confirmed_by'].every((key) => string(value[key])) && scope(value.scope_type) && integer(value.version) && value.version > 0 && timestamp(value.confirmed_at) && value.clock === 'real' && (value.scope_type === 'PROCESS' ? string(value.product_id) && string(value.route_version) : value.product_id === null && value.route_version === null)
function resolution(value: Record<string, unknown>) {
  if (value.resolution_version !== undefined && !nonnegative(value.resolution_version)) throw new Error('The objective resolution version could not be confirmed.')
  const item = value.coordination
  if (item !== undefined && item !== null && (!object(item) || !['coordination_id', 'confirmed_by', 'reason', 'context_hash'].every((key) => string(item[key])) || !timestamp(item.confirmed_at))) throw new Error('The source of the factory-wide coordination could not be confirmed.')
}
export function requiredBounds(order: ObjectiveName[]) {
  return { tardiness: order.indexOf('weighted_tardiness') > 0, overtime: order.slice(0, order.indexOf('incremental_overtime_metric')).some((name) => objectiveNames.indexOf(name) > 1) }
}
export function parseObjectiveDefinition(value: unknown): ObjectiveDefinition {
  if (!object(value) || !selection(value.selection) || !Array.isArray(value.objective_order) || value.objective_order.length !== 5 || new Set(value.objective_order).size !== 5 || !value.objective_order.every((name) => objectiveNames.some((known) => known === name)) || !(value.max_weighted_tardiness === null || nonnegative(value.max_weighted_tardiness)) || !(value.max_incremental_overtime_minutes === null || integer(value.max_incremental_overtime_minutes))) throw new Error('The objective order or limits are incomplete; check the scheduling preference.')
  const definition = value as unknown as ObjectiveDefinition
  if (definition.selection !== 'custom' && definition.objective_order.join() !== presetOrders[definition.selection].join()) throw new Error('The objective order does not match the selected preset; check it again.')
  const required = requiredBounds(definition.objective_order)
  if (required.tardiness && definition.max_weighted_tardiness === null) throw new Error('Set the weighted tardiness limit, in minutes.')
  if (required.overtime && definition.max_incremental_overtime_minutes === null) throw new Error('Set the added overtime limit, in staff-minutes.')
  return definition
}
export function parseEffectiveObjective(value: unknown): EffectiveObjective {
  if (!object(value) || !['READY', 'CONFLICT', 'INVALID'].includes(String(value.status)) || !(value.objective_version === null || string(value.objective_version)) || !Array.isArray(value.sources) || !value.sources.every(source) || !(value.context_hash === null || string(value.context_hash)) || !(value.reason === null || string(value.reason))) throw new Error('The current objective state could not be confirmed; refresh the scheduling preference.')
  if (value.definition !== null) parseObjectiveDefinition(value.definition)
  resolution(value)
  if (value.status === 'READY' && (!string(value.objective_version) || value.definition === null)) throw new Error('The current objective contract is missing its version or content; refresh to check.')
  return value as unknown as EffectiveObjective
}
export function parseObjectiveContracts(value: unknown): Record<string, ObjectiveContract> {
  if (!object(value)) throw new Error('The objective basis of the plan is incomplete; refresh to check.')
  for (const item of Object.values(value)) {
    if (!object(item) || !Array.isArray(item.sources) || !item.sources.every(source)) throw new Error('The objective sources of the plan could not be confirmed.')
    parseObjectiveDefinition(item.definition)
    resolution(item)
  }
  return value as Record<string, ObjectiveContract>
}
export function parsePreferenceProposal(value: unknown): PreferenceProposal {
  if (!object(value) || !['proposal_id', 'scope_id', 'proposer_id', 'reason'].every((key) => string(value[key])) || !scope(value.scope_type) || !['PENDING', 'CONFIRMED', 'REJECTED'].includes(String(value.state)) || !nonnegative(value.expected_version) || !timestamp(value.created_at)) throw new Error('The preference proposal format is incomplete; refresh to check.')
  parseObjectiveDefinition(value.definition)
  return value as unknown as PreferenceProposal
}
export function parsePreferenceState(value: unknown, factoryId: string): PreferenceState {
  if (!object(value) || !nonnegative(value.state_version) || !Array.isArray(value.heads) || !Array.isArray(value.proposals) || !Array.isArray(value.processes) || !Array.isArray(value.cases) || !Array.isArray(value.agent_proposals)) throw new Error('The scheduling preference data is incomplete; refresh to check.')
  parseEffectiveObjective(value.effective)
  for (const head of value.heads) {
    if (!object(head) || !scope(head.scope_type) || !string(head.scope_id) || (head.scope_type === 'FACTORY' && head.scope_id !== factoryId) || !integer(head.version) || head.version < 1 || typeof head.active !== 'boolean' || !string(head.preference_id)) throw new Error('The preference scope or version does not match; refresh to check.')
    parseObjectiveDefinition(head.definition)
  }
  for (const item of value.proposals) { const proposal = parsePreferenceProposal(item); if (proposal.scope_type === 'FACTORY' && proposal.scope_id !== factoryId) throw new Error('The proposal does not match the current factory.') }
  if (!value.processes.every((item) => object(item) && ['scope_id', 'product_id', 'name', 'route_version'].every((key) => string(item[key]))) || !value.cases.every((item) => object(item) && ['case_id', 'title', 'owner_id', 'state'].every((key) => string(item[key]))) || !value.agent_proposals.every((item) => object(item) && ['proposal_id', 'case_id', 'reason'].every((key) => string(item[key])) && selection(item.selection) && Array.isArray(item.missing_fields) && item.missing_fields.every(string))) throw new Error('The preference scope or suggestion data is incomplete; refresh to check.')
  return value as unknown as PreferenceState
}
export function parsePreferenceConfirmation(value: unknown, proposal: PreferenceProposal) {
  if (!object(value) || !string(value.preference_id) || !integer(value.version) || value.version < 1 || !nonnegative(value.state_version) || value.scope_type !== proposal.scope_type || value.scope_id !== proposal.scope_id) throw new Error('The preference confirmation does not match the original proposal; check the original request.')
  const actual = parseObjectiveDefinition(value.definition), expected = proposal.definition
  if (actual.selection !== expected.selection || actual.objective_order.join() !== expected.objective_order.join() || actual.max_weighted_tardiness !== expected.max_weighted_tardiness || actual.max_incremental_overtime_minutes !== expected.max_incremental_overtime_minutes) throw new Error('The objectives in the confirmation have changed; check the original request.')
  return value
}
export function parsePreferenceRejection(value: unknown, proposalId: string) {
  if (!object(value) || value.proposal_id !== proposalId || value.state !== 'REJECTED') throw new Error('The rejection receipt does not match the preference proposal; check the original request.')
  return value
}
export function parsePreferenceDeactivation(value: unknown, head: PreferenceHead) {
  if (!object(value) || value.scope_type !== head.scope_type || value.scope_id !== head.scope_id || value.active !== false || !nonnegative(value.state_version) || !integer(value.version) || value.version <= head.version) throw new Error('The deactivation receipt does not match the preference scope; check the original request.')
  return value
}
