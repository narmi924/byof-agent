import type { ExecutionSupport } from '../executionContracts'

export function declaredExecutionSupport(): ExecutionSupport {
  return { mode: 'CONDITIONAL', status: 'DECLARED', reason: 'The interface declares conditional release support.', observed_at: new Date().toISOString(), missing_capabilities: [], capabilities: {
    schema_version: 'byof.connector-capabilities/1', read_snapshot: true, read_changes: true, query_detail: true, accept_plan: true, query_action: true, idempotency: true, conditional_acceptance: true, snapshot_consistency: 'ATOMIC_SNAPSHOT',
  } }
}
