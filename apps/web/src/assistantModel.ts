import type { CandidateRecord } from './contracts'
import type { Learning, Priority } from './assistantContracts'

export const priorityNames: Record<Priority, string> = { delivery: 'On-time delivery', stability: 'Fewer changes', overtime: 'Less overtime' }
export const priorityMetrics: Record<Priority, string> = { delivery: 'weighted_tardiness', stability: 'changed_operations', overtime: 'incremental_overtime_metric' }

/** Only rank alternatives computed from the same facts and objective. Unknown metrics never win. */
export function rankCandidates(records: CandidateRecord[], learning: Learning | null): CandidateRecord[] {
  if (!learning?.active || records.length < 2) return records
  const reference = records[0]!.candidate.binding
  const peers = records.filter(r => r.candidate.binding.snapshot_hash === reference.snapshot_hash && r.candidate.binding.objective_version === reference.objective_version)
  const ranges = Object.entries(priorityMetrics).map(([key, metric]) => {
    const values = peers.flatMap(r => r.candidate.objective.filter(m => m.name === metric && m.value !== null).map(m => m.value!))
    return { key: key as Priority, metric, min: Math.min(...values), max: Math.max(...values) }
  })
  function score(r: CandidateRecord) {
    return ranges.reduce((sum, { key, metric, min, max }) => {
      const value = r.candidate.objective.find(m => m.name === metric)?.value
      return sum + learning!.weights[key] * (value == null ? 1 : max > min ? (value - min) / (max - min) : 0)
    }, 0)
  }
  return [...peers].sort((a, b) => score(a) - score(b)).concat(records.filter(r => !peers.includes(r)))
}
