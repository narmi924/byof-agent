import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { useState } from 'react'
import type { CalendarWindow, TimeSpan } from '../contracts'
import { dateTime, displayStatus } from '../presentation'
import { Badge } from '../ui'
import type { FactorySectionProps } from './sectionProps'

function Availability({ calendar, unavailable, zone }: { calendar: CalendarWindow[] | undefined; unavailable: TimeSpan[] | undefined; zone: string }) {
  if (!calendar) return <span className="muted">Shifts not provided</span>
  if (!calendar.length) return <span className="muted">No shifts</span>
  return <details className="factory-overview-calendar"><summary>{calendar.length} shift windows · {calendar.filter(item => item.kind === 'OVERTIME').length} overtime</summary>
    <ul>{calendar.map(item => <li key={`${item.kind}:${item.start_at}:${item.end_at}`}>{item.kind === 'NORMAL' ? 'Regular shift' : 'Overtime'}: {dateTime(item.start_at, zone)} — {dateTime(item.end_at, zone)}</li>)}</ul>
    {unavailable?.length ? <><p>Unavailable periods</p><ul>{unavailable.map(item => <li key={`${item.start_at}:${item.end_at}`}>{dateTime(item.start_at, zone)} — {dateTime(item.end_at, zone)}</li>)}</ul></> : null}
  </details>
}

export function FactoryCapacity({ snapshot, onEdit, disabled }: FactorySectionProps) {
  const [query, setQuery] = useState('')
  const zone = snapshot.profile.timezone
  const match = (value: string) => value.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
  const resources = snapshot.resources.filter(item => match(`${item.resource_id} ${item.operation_codes.join(' ')}`))
  const workers = snapshot.workers.filter(item => match(`${item.worker_id} ${item.skills.join(' ')}`))
  return <div className="factory-module-content">
    <div className="factory-overview-search"><label htmlFor="factory-capacity-search">Search machines, staff or skills</label><Input id="factory-capacity-search" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="ID, operation or skill" /><span>{resources.length} machines · {workers.length} staff</span></div>
    <section className="factory-overview-section" aria-label="Machines">
      <div className="factory-overview-section-heading"><div><h3>Machines</h3></div></div>
      {resources.length ? <div className="factory-overview-table"><table><caption>Machines · {resources.length}</caption><thead><tr><th>Machine</th><th>Operations</th><th>Status</th><th>Shifts</th><th>Action</th></tr></thead><tbody>{resources.map(item => <tr key={item.resource_id}>
        <th scope="row">{item.resource_id}</th><td>{item.operation_codes.join(', ')}</td><td>{item.status === 'AVAILABLE' ? displayStatus(item.status) : <Badge tone="negative">{displayStatus(item.status)}</Badge>}</td><td><Availability calendar={item.calendar} unavailable={item.unavailable} zone={zone} /></td>
        <td><div className="factory-overview-actions"><Button variant="outline" type="button" className="secondary" disabled={disabled} aria-label={`Edit machine ${item.resource_id}`} onClick={() => onEdit({ kind: 'resource', resourceId: item.resource_id })}>Machine status</Button><Button variant="outline" type="button" className="ghost" disabled={disabled} aria-label={`Overtime windows machine ${item.resource_id}`} onClick={() => onEdit({ kind: 'overtime', targetType: 'resource', targetId: item.resource_id })}>Overtime windows</Button></div></td>
      </tr>)}</tbody></table></div> : <p className="factory-overview-empty">{query && snapshot.resources.length ? 'No matching machines.' : 'No machine records.'}</p>}
    </section>
    <section className="factory-overview-section" aria-label="Staff">
      <div className="factory-overview-section-heading"><div><h3>Staff</h3></div></div>
      {workers.length ? <div className="factory-overview-table"><table><caption>Staff · {workers.length}</caption><thead><tr><th>Worker</th><th>Skills</th><th>Status</th><th>Overtime eligible</th><th>Shifts</th><th>Action</th></tr></thead><tbody>{workers.map(item => <tr key={item.worker_id}>
        <th scope="row">{item.worker_id}</th><td>{item.skills.join(', ')}</td><td>{item.status === 'AVAILABLE' ? displayStatus(item.status) : <Badge tone="negative">{displayStatus(item.status)}</Badge>}</td><td>{item.overtime_available ? 'Confirmed' : 'Not confirmed'}</td><td><Availability calendar={item.calendar} unavailable={item.unavailable} zone={zone} /></td>
        <td><div className="factory-overview-actions"><Button variant="outline" type="button" className="secondary" disabled={disabled} aria-label={`Edit worker ${item.worker_id}`} onClick={() => onEdit({ kind: 'worker', workerId: item.worker_id })}>Worker status</Button><Button variant="outline" type="button" className="ghost" disabled={disabled} aria-label={`Overtime windows worker ${item.worker_id}`} onClick={() => onEdit({ kind: 'overtime', targetType: 'worker', targetId: item.worker_id })}>Overtime windows</Button></div></td>
      </tr>)}</tbody></table></div> : <p className="factory-overview-empty">{query && snapshot.workers.length ? 'No matching staff.' : 'No staff records.'}</p>}
    </section>
  </div>
}
