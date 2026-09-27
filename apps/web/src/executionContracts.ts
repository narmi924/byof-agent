export interface ConnectorCapabilities {
  schema_version: 'byof.connector-capabilities/1'
  read_snapshot: boolean
  read_changes: boolean
  query_detail: boolean
  accept_plan: boolean
  query_action: boolean
  idempotency: boolean
  conditional_acceptance: boolean
  snapshot_consistency: 'ATOMIC_SNAPSHOT' | 'VERIFIED_WATERMARK' | 'UNVERIFIED'
}
export interface ExecutionSupport {
  mode: 'CONDITIONAL' | 'EXPORT_ONLY'
  status: 'DECLARED' | 'UNKNOWN' | 'INVALID' | 'STALE' | 'UNSUPPORTED'
  reason: string
  missing_capabilities: string[]
  observed_at: string | null
  capabilities: ConnectorCapabilities | null
}

const capabilityKeys = ['read_snapshot', 'read_changes', 'query_detail', 'accept_plan', 'query_action', 'idempotency', 'conditional_acceptance'] as const
const required = ['read_snapshot', 'accept_plan', 'query_action', 'idempotency', 'conditional_acceptance'] as const
const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const timestamp = (value: unknown): value is string => typeof value === 'string' && /(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value))

export function parseExecutionSupport(value: unknown): ExecutionSupport {
  const unknown: ExecutionSupport = { mode: 'EXPORT_ONLY', status: 'UNKNOWN', reason: 'The execution interface capabilities are not confirmed; ask the administrator to check the connection, then sync again.', missing_capabilities: [], observed_at: null, capabilities: null }
  if (value === undefined || value === null) return unknown
  const invalid: ExecutionSupport = { ...unknown, status: 'INVALID', reason: 'The execution interface capability declaration is not recognized; ask the administrator to check the connection.' }
  if (!object(value) || !['CONDITIONAL', 'EXPORT_ONLY'].includes(String(value.mode))
    || !['DECLARED', 'UNKNOWN', 'INVALID', 'STALE', 'UNSUPPORTED'].includes(String(value.status))
    || typeof value.reason !== 'string' || !value.reason.trim() || value.reason.length > 1000
    || !Array.isArray(value.missing_capabilities) || !value.missing_capabilities.every((item) => typeof item === 'string' && item.length <= 160)
    || (value.observed_at !== null && !timestamp(value.observed_at))) return invalid
  const caps = value.capabilities
  if (caps !== null && (!object(caps) || caps.schema_version !== 'byof.connector-capabilities/1'
    || !capabilityKeys.every((key) => typeof caps[key] === 'boolean')
    || !['ATOMIC_SNAPSHOT', 'VERIFIED_WATERMARK', 'UNVERIFIED'].includes(String(caps.snapshot_consistency))
    || (caps.conditional_acceptance && !caps.accept_plan)
    || (!caps.read_snapshot && caps.snapshot_consistency === 'ATOMIC_SNAPSHOT'))) return invalid
  if (value.mode === 'CONDITIONAL' && (value.status !== 'DECLARED' || !timestamp(value.observed_at)
    || !object(caps) || !required.every((key) => caps[key] === true) || caps.snapshot_consistency === 'UNVERIFIED'
    || value.missing_capabilities.length !== 0)) return invalid
  return value as unknown as ExecutionSupport
}

export function canPublishAutomatically(value: ExecutionSupport | undefined): boolean {
  return parseExecutionSupport(value).mode === 'CONDITIONAL'
}
