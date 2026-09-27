import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { factoryLocalToUtc, safeZone } from '../dayWindow'
import type { FactoryEditor } from '../factoryEditor'
import { dateTime } from '../presentation'
import type { FactoryEditorProps } from './editorTypes'
import { FactoryControlForm } from './FactoryControlForm'
import { readText } from './termsValue'

type Props = FactoryEditorProps & { selection: Extract<FactoryEditor, { kind: 'overtime' }> }

export function FactoryOvertimeEditor({ snapshot, disabled, command, onError, selection }: Props) {
  const { targetType, targetId } = selection
  const entries = targetType === 'worker' ? snapshot.workers.map(item => ({ id: item.worker_id, ...item })) : snapshot.resources.map(item => ({ id: item.resource_id, ...item }))
  const target = entries.find(item => item.id === targetId)
  const zone = safeZone(snapshot.profile.timezone)
  const version = target?.version
  return <FactoryControlForm key={`${targetType}:${target?.id}:${version}`} id="factory-overtime-windows" title="Overtime availability of machines and staff" disabled={disabled} onError={onError} onSubmit={data => {
    if (!target || version === undefined) throw new Error('The resource version is missing; refresh the shop floor before saving.')
    const start = factoryLocalToUtc(readText(data, 'start'), zone), end = factoryLocalToUtc(readText(data, 'end'), zone)
    if (Date.parse(start) <= Date.parse(snapshot.snapshot_clock) || Date.parse(end) <= Date.parse(start)) throw new Error('The overtime window must be after the current factory time and end after it starts.')
    if (snapshot.horizon && (Date.parse(start) < Date.parse(snapshot.horizon.start_at) || Date.parse(end) > Date.parse(snapshot.horizon.end_at))) throw new Error('The overtime window must be inside the current scheduling window.')
    if (target.calendar?.some(item => Date.parse(start) < Date.parse(item.end_at) && Date.parse(end) > Date.parse(item.start_at))) throw new Error('The new window overlaps an existing shift; check and enter it again.')
    command('overtime_window.set', { target_type: targetType, target_id: target.id, expected_version: version, action: 'add', start_at: start, end_at: end }, `Record overtime availability for ${target.id}`)
  }}>
    <p className="muted">Adding a window for a worker also confirms their overtime eligibility; overtime in a plan still needs manager approval.</p>
    <p className="muted">Current factory time: {dateTime(snapshot.snapshot_clock, zone)}.{snapshot.horizon ? ` Allowed range: ${dateTime(snapshot.horizon.start_at, zone)} — ${dateTime(snapshot.horizon.end_at, zone)}.` : ''}</p>
    <p>{targetType === 'worker' ? 'Worker' : 'Machine'}: {targetId}{!target ? ' · no longer exists; close and check the latest shop floor' : ''}</p>
    {version === undefined ? <p className="notice">The source version of this object is missing; refresh the shop floor before saving.</p> : null}
    {version !== undefined ? <p className="muted">Source version {version}{target && 'overtime_available' in target ? `; overtime eligibility: ${target.overtime_available ? 'confirmed' : 'not confirmed'}` : ''}.</p> : null}
    {!target?.calendar?.length ? <p className="muted">No shift windows to show.</p> : <ul className="business-window-list">{target.calendar.map(item => <li key={`${item.kind}:${item.start_at}:${item.end_at}`}><span>{item.kind === 'NORMAL' ? 'Regular shift (read-only)' : 'Overtime available'} · {dateTime(item.start_at, zone)} — {dateTime(item.end_at, zone)}</span>{item.kind === 'OVERTIME' ? <Button variant="outline" type="button" className="secondary" disabled={version === undefined || Date.parse(item.start_at) <= Date.parse(snapshot.snapshot_clock)} onClick={() => {
      if (version !== undefined) command('overtime_window.set', { target_type: targetType, target_id: target.id, expected_version: version, action: 'remove', start_at: item.start_at, end_at: item.end_at }, `Remove overtime window of ${target.id}`)
    }}>Remove this overtime window</Button> : null}</li>)}</ul>}
    <p className="muted">Regular shifts and overtime windows that have started or passed cannot be changed here.</p>
    <label>Overtime start ({zone})<Input name="start" type="datetime-local" step="60" required /></label>
    <label>Overtime end ({zone})<Input name="end" type="datetime-local" step="60" required /></label>
    <Button variant="outline" type="submit" disabled={!target || version === undefined}>Record overtime availability</Button>
  </FactoryControlForm>
}
