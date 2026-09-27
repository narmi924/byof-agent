import { Input } from '../components/ui/input'
import { useState } from 'react'
import { FactoryControlForm } from './FactoryControlForm'
import { FactoryOvertimeEditor } from './FactoryOvertimeEditor'
import { integer } from './formValue'
import type { FactoryEditorProps } from './editorTypes'

export function FactoryCapacityEditor({ snapshot, selection, disabled, zone, status, command, onError }: FactoryEditorProps) {
  const [resourceAction, setResourceAction] = useState('down')
  const [workerAction, setWorkerAction] = useState('leave')
  if (selection.kind === 'overtime') return <FactoryOvertimeEditor snapshot={snapshot} selection={selection} disabled={disabled} zone={zone} status={status} command={command} onError={onError} />

  if (selection.kind === 'resource') {
    const resource = snapshot.resources.find(item => item.resource_id === selection.resourceId)
    if (!resource) return null
    return <FactoryControlForm id="factory-resource-status" title="Machine status" button="Confirm machine status" disabled={disabled} onError={onError} onSubmit={data => {
      if (resourceAction === 'outage') {
        if (resource.status !== 'AVAILABLE') throw new Error('A temporary stop can only be recorded for an available machine.')
        command('resource.outage', { resource_id: resource.resource_id, minutes: integer(data, 'minutes', 1, 240) }, 'Temporary stop')
        return
      }
      const restore = data.get('action') === 'restore'
      command(restore ? 'resource.restore' : 'resource.down', { resource_id: resource.resource_id }, restore ? 'Machine restored' : 'Machine down')
    }}>
      <p>Machine: {resource.resource_id}</p>
      <label>Machine action<select name="action" value={resourceAction} onChange={event => setResourceAction(event.target.value)}><option value="down">Down (recovery time unknown)</option><option value="restore">Restore</option><option value="outage">Temporary stop (known duration)</option></select></label>
      {resourceAction === 'outage' ? <label>Stop duration (minutes)<Input name="minutes" type="number" min="1" max="240" step="1" defaultValue="30" required /></label> : null}
      <p className="muted">Restoring a machine does not resume blocked operations by itself; the shop floor must confirm the remaining work.</p>
    </FactoryControlForm>
  }

  if (selection.kind !== 'worker') return null
  const worker = snapshot.workers.find(item => item.worker_id === selection.workerId)
  if (!worker) return null
  const working = snapshot.actuals.find(item => item.worker_id === worker.worker_id && ['SETUP', 'IN_PROGRESS'].includes(item.state))
  return <FactoryControlForm id="factory-worker-status" title="Worker status" button="Confirm worker status" disabled={disabled} onError={onError} onSubmit={data => {
    if (workerAction === 'leave') {
      if (worker.status !== 'AVAILABLE') throw new Error('Temporary leave can only be recorded for a worker on duty.')
      command('worker.leave', { worker_id: worker.worker_id, minutes: integer(data, 'minutes', 1, 240) }, 'Temporary leave')
      return
    }
    const returning = workerAction === 'return'
    command(returning ? 'worker.return' : 'worker.absent', { worker_id: worker.worker_id }, returning ? 'Worker back' : 'Worker absent')
  }}>
    <p>Worker: {worker.worker_id}{working ? ` · working on ${working.operation_id}` : ' · currently idle'}</p>
    <label>Worker action<select name="action" value={workerAction} onChange={event => setWorkerAction(event.target.value)}><option value="leave">Temporary leave (known return time)</option><option value="absent">Absent (return time unknown)</option><option value="return">Confirm return</option></select></label>
    {workerAction === 'leave' ? <label>Leave duration (minutes)<Input name="minutes" type="number" min="1" max="240" step="1" defaultValue="60" required /></label> : null}
    {workerAction === 'absent' && working ? <p className="muted">The operation this worker is running can only be continued by them; with an unknown return time the manager can only wait. Choose temporary leave for a known duration.</p> : null}
  </FactoryControlForm>
}
