import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { useState } from 'react'
import { dateTime, displayStatus, operationNames } from '../presentation'
import { Badge } from '../ui'
import type { FactorySectionProps } from './sectionProps'

export function FactoryExecution({ snapshot, onEdit, disabled }: FactorySectionProps) {
  const [query, setQuery] = useState('')
  const [focus, setFocus] = useState('all')
  const zone = snapshot.profile.timezone
  const operations = operationNames(snapshot)
  const blocked = snapshot.actuals.filter(item => item.state === 'BLOCKED')
  const completed = snapshot.actuals.filter(item => item.state === 'COMPLETED')
  const pendingQuality = completed.filter(item => !['PASSED', 'FAILED'].includes(item.quality_state))
  const visible = snapshot.actuals.filter(item => {
    if (focus === 'blocked' && item.state !== 'BLOCKED') return false
    if (focus === 'quality' && (item.state !== 'COMPLETED' || item.quality_state === 'PASSED')) return false
    const name = operations.get(item.operation_id)
    return `${item.operation_id} ${name?.batchId ?? item.batch_id ?? ''} ${name?.name ?? ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
  })
  const visibleSegments = visible.filter(item => item.segments?.length)

  return <div className="factory-module-content">
    <section className="factory-overview-section" aria-label="Execution and quality">
      <div className="factory-overview-section-heading"><div><h3>Operation progress and quality</h3><p>{snapshot.actuals.length} operations recorded · {blocked.length} blocked · {completed.length} completed · {pendingQuality.length} awaiting QC</p></div></div>
      {snapshot.actuals.length ? <div className="factory-overview-search"><label htmlFor="factory-operation-search">Search batches or operations</label><Input id="factory-operation-search" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Batch ID or operation" /><label htmlFor="factory-operation-focus">Show</label><select id="factory-operation-focus" value={focus} onChange={event => setFocus(event.target.value)}><option value="all">All operations</option><option value="blocked">Blocked operations</option><option value="quality">Awaiting QC or quality issues</option></select><span>{visible.length} / {snapshot.actuals.length}</span></div> : null}
      {visible.length ? <div className="factory-overview-table"><table><caption>Shop floor execution records · {visible.length}</caption><thead><tr><th>Batch / operation</th><th>Status</th><th>Completed</th><th>Remaining production / changeover</th><th>Quality</th><th>Action</th></tr></thead><tbody>{visible.map(item => <tr key={item.operation_id}>
        <th scope="row">{operations.get(item.operation_id)?.batchId ?? item.batch_id ?? item.operation_id}<span className="cell-note">{operations.get(item.operation_id)?.name ?? item.operation_id}</span></th>
        <td>{item.state === 'BLOCKED' ? <Badge tone="negative">{displayStatus(item.state)}</Badge> : displayStatus(item.state)}</td><td>{item.completed_quantity} pcs</td>
        <td>{item.remaining_minutes == null ? 'Not confirmed' : `${item.remaining_minutes} min`} / {item.remaining_setup_minutes == null ? 'Not confirmed' : `${item.remaining_setup_minutes} min`}</td>
        <td>{item.quality_state === 'FAILED' ? 'Failed' : displayStatus(item.quality_state)}</td>
        <td>{item.state === 'BLOCKED' ? <Button variant="outline" type="button" className="secondary" disabled={disabled} aria-label={`Confirm remaining ${item.operation_id}`} onClick={() => onEdit({ kind: 'remaining', operationId: item.operation_id })}>Confirm remaining work</Button> : item.state === 'COMPLETED' ? <Button variant="outline" type="button" className="secondary" disabled={disabled} aria-label={`Record quality ${item.operation_id}`} onClick={() => onEdit({ kind: 'quality', operationId: item.operation_id })}>Record quality check</Button> : 'Running'}</td>
      </tr>)}</tbody></table></div> : <p className="factory-overview-empty">{snapshot.actuals.length ? 'No operations match the current filter.' : 'No execution records.'}</p>}
      {visibleSegments.length ? <details className="factory-overview-details"><summary>Execution segments</summary><div className="factory-overview-table"><table><caption>Recorded execution time per operation</caption><thead><tr><th>Operation</th><th>Segments</th></tr></thead><tbody>{visibleSegments.map(item => <tr key={item.operation_id}><th scope="row">{operations.get(item.operation_id)?.name ?? item.operation_id}</th><td><ol className="factory-overview-segments">{item.segments?.map((segment, index) => <li key={`${segment.source_event_id}:${index}`}>{segment.phase === 'SETUP' ? 'Changeover' : 'Production'}: {dateTime(segment.start_at, zone)} — {dateTime(segment.end_at, zone)}</li>)}</ol></td></tr>)}</tbody></table></div></details> : null}
    </section>
  </div>
}
