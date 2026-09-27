export interface RiskSuggestion {
  suggestion_id: string
  run_id: string
  source_revision: string
  title: string
  detail: string
  prompt: string
}

export interface RiskSuggestions {
  run_id: string | null
  freshness: 'CURRENT' | 'STALE' | 'UNKNOWN'
  suggestions: RiskSuggestion[]
}

export function parseRiskSuggestions(value: unknown, factoryRunId: string | undefined): RiskSuggestions {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error('The shop floor suggestion is not recognized.')
  const record = value as Record<string, unknown>
  if ((record.run_id !== null && typeof record.run_id !== 'string')
    || !['CURRENT', 'STALE', 'UNKNOWN'].includes(String(record.freshness))
    || !Array.isArray(record.suggestions)
    || !record.suggestions.every(item => typeof item === 'object' && item !== null && !Array.isArray(item)
      && typeof item.suggestion_id === 'string' && /^[0-9a-f]{64}$/.test(item.suggestion_id)
      && typeof item.run_id === 'string' && item.run_id === record.run_id
      && typeof item.source_revision === 'string'
      && typeof item.title === 'string' && item.title.length > 0
      && typeof item.detail === 'string' && item.detail.length > 0
      && typeof item.prompt === 'string' && item.prompt.length > 0)) throw new Error('The shop floor suggestion data is incomplete.')
  if (factoryRunId && record.run_id !== factoryRunId) return { run_id: factoryRunId, freshness: 'STALE', suggestions: [] }
  return record as unknown as RiskSuggestions
}
