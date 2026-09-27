import { useEffect, useState } from 'react'
import { errorMessage, readAssistant } from './api'
import type { AssistantAction } from './assistantContracts'
import { quotedCost } from './businessContracts'
import { plainBusinessText, shortDateTime } from './businessText'
import type { Publication, Snapshot } from './contracts'
import { Badge, EmptyState, MessageStrip } from './ui'
import { measureNames } from './copy'

const stageNames: Record<string, string> = { PREPARE: 'Preparing', APPLY: 'Applying measures', SOLVING: 'Checking the schedule', PUBLISHING: 'Releasing to the factory' }
const factoryStates: Record<Publication['release']['source_state'], string> = {
  PENDING_SOURCE: 'Waiting for the factory', UNKNOWN: 'Checking the factory receipt', ACCEPTED_PENDING_EFFECTIVE: 'Accepted by factory, not yet effective', ACTIVE: 'Accepted by factory', REJECTED: 'Rejected by factory',
}
const executionStates: Record<Publication['release']['execution_state'], string> = { NOT_STARTED: 'Not started', IN_PROGRESS: 'In production', COMPLETED: 'Completed', BLOCKED: 'Execution blocked' }

type Option = { title?: string; actions?: { kind: string; target_id: string; quantity: number }[]; economics?: { status: string; incremental_cash_outlay_minor: number | null } | null }
const text = (value: unknown) => typeof value === 'string' ? value : ''

/** One row per manager decision: what was approved, what it cost, and where execution stands. */
export function ExecutionRecords({ factoryId, snapshot, publications }: { factoryId: string; snapshot: Snapshot | null; publications: Publication[] }) {
  const [actions, setActions] = useState<AssistantAction[] | null>(null)
  const [error, setError] = useState('')
  const zone = snapshot?.profile.timezone
  useEffect(() => {
    let stopped = false
    let timer: number | undefined
    let controller: AbortController | undefined
    async function load() {
      controller = new AbortController()
      const timeout = window.setTimeout(() => controller?.abort(), 10_000)
      try {
        const state = await readAssistant(factoryId, controller.signal)
        if (!stopped) { setActions(state.actions); setError('') }
      } catch (reason) {
        if (!stopped) setError(errorMessage(reason, 'Reading the execution log was interrupted; retrying.'))
      } finally {
        window.clearTimeout(timeout)
        if (!stopped) timer = window.setTimeout(() => { void load() }, 5000)
      }
    }
    void load()
    return () => { stopped = true; controller?.abort(); window.clearTimeout(timer) }
  }, [factoryId])
  const decisions = (actions ?? []).filter(a => (a.kind === 'approve' || a.kind === 'treatment_execute') && (!snapshot?.run_id || a.run_id === snapshot.run_id)).sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 30)
  return <section className="execution-records" aria-labelledby="records-title">
    <h1 id="records-title">Execution log</h1>
    {error ? <MessageStrip tone="critical"><p>{error}</p></MessageStrip> : null}
    {actions === null && !error ? <div className="view-skeleton" role="status" aria-label="Loading"><span /><span /><span /></div> : null}
    {actions !== null && !decisions.length ? <EmptyState title="No executions yet" /> : null}
    <ol className="record-list">{decisions.map(action => {
      const option = (action.kind === 'treatment_execute' ? action.payload.option : null) as Option | null
      const releaseId = text(action.result?.release_id)
      const publication = publications.find(item => item.release.release_id === releaseId)
      const cash = option?.economics?.status === 'ESTIMATED' ? option.economics.incremental_cash_outlay_minor : null
      const state = action.state === 'DONE' ? publication ? factoryStates[publication.release.source_state] : 'Completed'
        : action.state === 'CANCELLED' ? 'Remaining steps cancelled' : action.state === 'FAILED' ? 'Not completed' : action.state === 'ATTENTION' ? 'Needs checking' : stageNames[text(action.result?.stage)] ?? 'Executing'
      const tone = action.state === 'FAILED' || action.state === 'ATTENTION' ? 'negative' : action.state === 'DONE' ? 'positive' : 'info'
      return <li key={action.action_id} className="record-item">
        <div className="record-head">
          <span className="eyebrow">Approved {shortDateTime(action.created_at, zone)}</span>
          <Badge tone={tone}>{state}</Badge>
        </div>
        <h2>{option?.title ?? 'Schedule plan'}</h2>
        {option?.actions?.length ? <ul className="record-measures">{option.actions.map((item, index) => <li key={index}>{measureNames[item.kind] ?? item.kind} · {item.target_id}{item.quantity > 0 ? ` · ${item.quantity}` : ''}</li>)}</ul> : <p className="muted">No extra purchases or resource measures</p>}
        <dl className="record-facts">
          <div><dt>New cash outlay</dt><dd>{cash === null ? action.kind === 'approve' ? 'None' : 'Not costed' : quotedCost(cash, 'SGD')}</dd></div>
          <div><dt>Production progress</dt><dd>{publication ? executionStates[publication.release.execution_state] : 'Not released yet'}</dd></div>
        </dl>
        {text(action.result?.summary) ? <p className="muted">{plainBusinessText(text(action.result?.summary), zone)}</p> : null}
      </li>
    })}</ol>
  </section>
}
