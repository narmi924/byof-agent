export interface ModelSelection {
  selected_model_id: string
  version: number
  models: { model_id: string; label: string; available: boolean }[]
  scope: 'CURRENT_USER'
  takes_effect: 'NEXT_TURN'
}

export function parseModelSelection(value: unknown): ModelSelection {
  const v = value as ModelSelection | null
  if (!v || typeof v.selected_model_id !== 'string' || !Number.isInteger(v.version) || v.version < 0
    || v.scope !== 'CURRENT_USER' || v.takes_effect !== 'NEXT_TURN' || !Array.isArray(v.models)
    || !v.models.every(m => m && typeof m.model_id === 'string' && typeof m.label === 'string' && typeof m.available === 'boolean')
    || new Set(v.models.map(m => m.model_id)).size !== v.models.length) throw new Error('The model configuration is not recognized; refresh and try again.')
  return v
}
