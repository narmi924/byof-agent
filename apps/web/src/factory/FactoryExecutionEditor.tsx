import { Input } from '../components/ui/input'
import { operationNames } from '../presentation'
import { FactoryControlForm } from './FactoryControlForm'
import { integer } from './formValue'
import type { FactoryEditorProps } from './editorTypes'

export function FactoryExecutionEditor({ snapshot, selection, disabled, command, onError }: FactoryEditorProps) {
  if (selection.kind !== 'remaining' && selection.kind !== 'quality') return null
  const operation = snapshot.actuals.find(item => item.operation_id === selection.operationId)
  const name = operationNames(snapshot).get(selection.operationId)?.name ?? 'Operation'

  if (selection.kind === 'remaining') return operation?.state === 'BLOCKED' ? <FactoryControlForm title="Confirm remaining work after the interruption" button="Confirm remaining work" disabled={disabled} onError={onError} onSubmit={data => {
    command('execution.confirm_remaining', { operation_id: operation.operation_id, remaining_minutes: integer(data, 'remaining', 1), remaining_setup_minutes: integer(data, 'setup', 0) }, 'Remaining work')
  }}>
    <p>Operation: {operation.operation_id} · {name}</p>
    <label>Remaining production minutes<Input name="remaining" type="number" min="1" step="1" required /></label>
    <label>Remaining changeover minutes<Input name="setup" type="number" min="0" step="1" required /></label>

  </FactoryControlForm> : null

  if (operation?.state !== 'COMPLETED') return null
  return <div className="factory-editor-form-stack">
    <FactoryControlForm title="Quality check result" button="Record result" disabled={disabled} onError={onError} onSubmit={data => {
      const qualityState = String(data.get('quality'))
      if (!['FAILED', 'PASSED', 'UNKNOWN'].includes(qualityState)) throw new Error('Choose a check result.')
      const evidence = String(data.get('evidence') ?? '').trim()
      if (operation.quality_state === 'FAILED' && qualityState === 'PASSED' && !evidence) throw new Error('Enter the evidence for the recheck or rework pass.')
      command('quality.record', { operation_id: operation.operation_id, quality_state: qualityState, ...(evidence ? { evidence } : {}) }, 'Quality check')
    }}>
      <p>Operation: {operation.operation_id} · {name}</p>
      <label>Check result<select name="quality" defaultValue="" required><option value="">Choose a result</option><option value="FAILED">Failed</option><option value="PASSED">Passed</option><option value="UNKNOWN">Pending</option></select></label>
      {operation.quality_state === 'FAILED' ? <label>Evidence for the recheck or rework pass<Input name="evidence" type="text" maxLength={500} placeholder="Required when changing to passed" /></label> : null}
    </FactoryControlForm>
    {operation.quality_state === 'FAILED' ? <FactoryControlForm title="Unrecoverable batch" button="Confirm scrap and remake" disabled={disabled} onError={onError} onSubmit={data => {
      if (data.get('confirm_scrap') !== 'on') throw new Error('Confirm the scrap of the whole batch and the remake impact.')
      command('quality.scrap', { operation_id: operation.operation_id, reason: String(data.get('reason')).trim() }, 'Scrap and remake')
    }}>
      <p>Use only when the batch cannot pass a recheck or rework. Production and check records are kept; unfinished customer demand becomes new batches to schedule.</p>
      <label>Scrap evidence<Input name="reason" type="text" maxLength={500} required placeholder="e.g. recheck confirmed not repairable" /></label>
      <label className="checkbox-label"><Input name="confirm_scrap" type="checkbox" required />Confirm the batch cannot be recovered; after scrapping, unfinished customer demand becomes new batches to schedule</label>
    </FactoryControlForm> : null}
  </div>
}
