import { Button } from './components/ui/button'
import { usePendingSlot } from './pendingJournal'
import type { ManagementAttempt } from './pendingJournal'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, definitiveRejection, commandSimulator, errorMessage, readSimulator, replaySimulator, todayRunSimulator } from './api'
import type { FinishedGoodsLot, SimulatorCommand, SimulatorStatus, Snapshot } from './contracts'
import { dayKeyOf, safeZone } from './dayWindow'
import { dateTime } from './presentation'
import { FactoryOverview } from './FactoryOverview'
import type { FactoryChange } from './FactoryOverview'
import { FactoryEditorDialog } from './FactoryEditorDialog'
import { FactoryBusinessEditor } from './factory/FactoryBusinessEditor'
import { SimulatorControls } from './factory/SimulatorControls'
import type { FactoryEditor } from './factoryEditor'

function targetFingerprint(selection: FactoryEditor, snapshot: Snapshot): string | null {
  let target: unknown
  switch (selection.kind) {
    case 'order': target = snapshot.orders.find(item => item.order_id === selection.orderId && ['CONFIRMED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'].includes(item.status)); break
    case 'inventory': target = snapshot.inventory.find(item => item.material_id === selection.materialId); break
    case 'receipt': target = snapshot.receipts.find(item => item.receipt_id === selection.receiptId && ['EXPECTED', 'CONFIRMED'].includes(item.status)); break
    case 'resource': target = snapshot.resources.find(item => item.resource_id === selection.resourceId); break
    case 'worker': target = snapshot.workers.find(item => item.worker_id === selection.workerId); break
    case 'remaining': target = snapshot.actuals.find(item => item.operation_id === selection.operationId && item.state === 'BLOCKED'); break
    case 'quality': target = snapshot.actuals.find(item => item.operation_id === selection.operationId && item.state === 'COMPLETED'); break
    case 'delivery-rule': target = snapshot.profile.products.some(item => item.product_id === selection.productId) ? { productId: selection.productId, terms: snapshot.business_terms } : undefined; break
    case 'quote': {
      const receipt = snapshot.receipts.find(item => item.receipt_id === selection.receiptId)
      const quote = snapshot.business_terms?.expedite_quotes.find(item => item.quote_id === selection.quoteId && item.receipt_id === selection.receiptId)
      target = (selection.quoteId ? quote : receipt) ? { receipt: receipt ?? null, quote, terms: snapshot.business_terms } : undefined
      break
    }
    case 'overtime': target = selection.targetType === 'worker' ? snapshot.workers.find(item => item.worker_id === selection.targetId) : snapshot.resources.find(item => item.resource_id === selection.targetId); break
    case 'new-order': target = snapshot.profile.products; break
    case 'new-receipt': target = !selection.materialId || snapshot.profile.materials.some(item => item.material_id === selection.materialId) ? snapshot.profile.materials : undefined; break
  }
  return target === undefined ? null : JSON.stringify([snapshot.run_id, snapshot.profile.version, target])
}

const changeNames: Partial<Record<SimulatorCommand['kind'], string>> = {
  'resource.down': 'Machine down', 'resource.outage': 'Machine down', 'resource.restore': 'Machine restored',
  'worker.absent': 'Worker absent', 'worker.leave': 'Temporary leave', 'worker.return': 'Worker back',
  'receipt.delay': 'Receipt delayed', 'receipt.shortfall': 'Receipt short', 'receipt.cancel': 'Receipt cancelled', 'receipt.receive': 'Goods received', 'receipt.add': 'Receipt added',
  'inventory.reconcile': 'Stock count', 'order.add': 'Order added', 'order.revise': 'Order changed',
  'quality.record': 'Quality check', 'quality.scrap': 'Batch scrapped', 'execution.confirm_remaining': 'Remaining work confirmed',
  'delivery_rule.set': 'Split-delivery rule', 'expedite_quote.set': 'Expedite quote', 'expedite_quote.remove': 'Quote removed', 'overtime_window.set': 'Overtime window',
}

/** A short business description of a confirmed exception; clock and demo controls are not listed. */
function describeChange(command: SimulatorCommand, before: Snapshot | null): string | null {
  const name = changeNames[command.kind]
  if (!name) return null
  const p = command.payload
  const target = [p.order_id, p.resource_id, p.worker_id, p.receipt_id, p.material_id].find(value => typeof value === 'string')
  const order = before?.orders.find(item => item.order_id === p.order_id)
  // A moved date is part of the order change, not a detail to leave out.
  const due = command.kind === 'order.revise' && typeof p.due_at === 'string' && order && Date.parse(p.due_at) !== Date.parse(order.due_at)
    ? `, due ${dateTime(p.due_at, safeZone(before?.profile.timezone)).slice(5)}` : ''
  const detail = command.kind === 'order.revise' && typeof p.quantity === 'number' ? ` → ${p.quantity} pcs${due}`
    : ['resource.outage', 'worker.leave'].includes(command.kind) && typeof p.minutes === 'number' ? ` · ${p.minutes} min`
      : command.kind === 'order.add' && typeof p.quantity === 'number' ? ` · ${p.quantity} pcs` : ''
  return `${name}${target ? ` · ${target}` : ''}${detail}`
}

function readChanges(key: string): FactoryChange[] {
  try { return JSON.parse(sessionStorage.getItem(key) ?? '[]') as FactoryChange[] } catch { return [] }
}

function editorLabel(selection: FactoryEditor): string {
  switch (selection.kind) {
    case 'order': return `Order ${selection.orderId}`
    case 'new-order': return 'New order'
    case 'inventory': return `Material ${selection.materialId} · Stock count`
    case 'receipt': return `Receipt ${selection.receiptId}`
    case 'new-receipt': return 'Record a confirmed receipt'
    case 'resource': return `Machine ${selection.resourceId}`
    case 'worker': return `Worker ${selection.workerId}`
    case 'remaining': return `Operation ${selection.operationId} · Remaining work`
    case 'quality': return `Operation ${selection.operationId} · Quality check`
    case 'delivery-rule': return `Product ${selection.productId} · Split-delivery rule`
    case 'quote': return `Receipt ${selection.receiptId} · ${selection.quoteId ? `Quote ${selection.quoteId}` : 'New quote'}`
    case 'overtime': return `${selection.targetType === 'worker' ? 'Worker' : 'Machine'} ${selection.targetId} · Overtime availability`
  }
}

export function SimulatorPanel({ factoryId, userId, snapshot: currentSnapshot, finishedGoods, onChanged, onSessionEnded }: { factoryId: string; userId?: string | undefined; snapshot: Snapshot | null; finishedGoods?: FinishedGoodsLot[] | null | undefined; onChanged: () => Promise<void>; onSessionEnded: () => void }) {
  const [status, setStatus] = useState<SimulatorStatus | null>(null)
  const [readError, setReadError] = useState('')
  const [actionError, setActionError] = useState('')
  const [factsReadError, setFactsReadError] = useState('')
  const [factsRefreshing, setFactsRefreshing] = useState(false)
  const [message, setMessage] = useState('')
  const [editor, setEditor] = useState<{ selection: FactoryEditor; snapshot: Snapshot; generation: number } | null>(null)
  const [dirty, setDirty] = useState(false)
  const [changeVersion, setChangeVersion] = useState(0)
  const selection = editor?.selection
  // Keep displayed facts and their versions together; only the validation clock advances.
  const snapshot = editor ? { ...editor.snapshot, snapshot_clock: currentSnapshot?.snapshot_clock ?? editor.snapshot.snapshot_clock } : currentSnapshot
  const [busy, setBusy] = useState(false)
  const [wallNow, setWallNow] = useState(() => Date.now())
  const journal = usePendingSlot('simulator', userId, factoryId)
  const pending = journal.pending
  const mounted = useRef(false)
  const reading = useRef<AbortController | null>(null)
  const mutation = useRef<AbortController | null>(null)
  const readTimer = useRef<number | undefined>(undefined)
  const pollTimer = useRef<number | undefined>(undefined)
  const actionTimer = useRef<number | undefined>(undefined)
  const factsReadGeneration = useRef(0)
  const refresh = useCallback(async function load() {
    window.clearTimeout(pollTimer.current)
    window.clearTimeout(readTimer.current)
    reading.current?.abort()
    const controller = new AbortController()
    reading.current = controller
    readTimer.current = window.setTimeout(() => {
      controller.abort()
      if (mounted.current) { setStatus(null); setReadError('Reading the run status timed out; refresh to check.') }
    }, 10_000)
    try {
      const result = await readSimulator(factoryId, controller.signal)
      if (mounted.current && !controller.signal.aborted) { setStatus(result); setReadError('') }
    } catch (reason) {
      if (mounted.current && !controller.signal.aborted) {
        setStatus(null); setReadError(errorMessage(reason, 'Cannot read the run status; refresh to check.'))
        if (reason instanceof ApiError && reason.status === 401) onSessionEnded()
      }
    } finally {
      if (reading.current === controller) {
        window.clearTimeout(readTimer.current)
        if (mounted.current) pollTimer.current = window.setTimeout(() => { void load() }, 3000)
      }
    }
  }, [factoryId, onSessionEnded])
  useEffect(() => {
    mounted.current = true
    void refresh()
    return () => { mounted.current = false; reading.current?.abort(); mutation.current?.abort(); window.clearTimeout(readTimer.current); window.clearTimeout(pollTimer.current); window.clearTimeout(actionTimer.current) }
  }, [refresh])
  useEffect(() => { const timer = window.setInterval(() => setWallNow(Date.now()), 30_000); return () => window.clearInterval(timer) }, [])
  useEffect(() => {
    if (message || actionError || factsReadError) document.getElementById('factory-action-feedback')?.focus()
  }, [message, actionError, factsReadError])

  function refreshWorkspace(closeAfterSuccess = false) {
    const generation = ++factsReadGeneration.current
    setFactsRefreshing(true)
    void onChanged().then(() => {
      if (mounted.current && generation === factsReadGeneration.current) { setFactsReadError(''); if (closeAfterSuccess) setEditor(null) }
    }).catch(() => {
      if (mounted.current && generation === factsReadGeneration.current) setFactsReadError('The shop floor data did not refresh; check the latest data shortly.')
    }).finally(() => {
      if (mounted.current && generation === factsReadGeneration.current) setFactsRefreshing(false)
    })
  }
  async function send(attempt: ManagementAttempt) {
    if (mutation.current) return
    const recovering = pending !== null
    if (!journal.save(attempt)) return
    const controller = new AbortController()
    mutation.current = controller
    setBusy(true); setActionError(''); setFactsReadError(''); setMessage('')
    actionTimer.current = window.setTimeout(() => {
      controller.abort()
      if (mounted.current) { setBusy(false); setActionError('The result is not confirmed yet; check the original request.'); mutation.current = null; void refresh(); refreshWorkspace() }
    }, 15_000)
    let confirmed = false
    try {
      if ('replay' in attempt) await replaySimulator(factoryId, attempt.replay, controller.signal)
      else if ('today' in attempt) await todayRunSimulator(factoryId, attempt.today, controller.signal)
      else await commandSimulator(factoryId, attempt.command, controller.signal)
      if (!mounted.current || controller.signal.aborted) return
      if ('replay' in attempt || 'today' in attempt) setStatus(null)
      if (!journal.clear(attempt)) return; confirmed = true; setDirty(false); setMessage(`${attempt.label} confirmed.`)
      const command = 'command' in attempt ? attempt.command : null
      const change = command ? describeChange(command, snapshot) : null
      if (command && change) {
        const key = `byof.changes:${factoryId}:${command.run_id}`
        const next = [{ at: status?.business_clock ?? new Date().toISOString(), label: change }, ...readChanges(key)].slice(0, 30)
        try { sessionStorage.setItem(key, JSON.stringify(next)) } catch { /* The list is a convenience only. */ }
        setChangeVersion(value => value + 1)
      }
    } catch (reason) {
      if (!mounted.current || controller.signal.aborted) return
      if (definitiveRejection(reason, recovering)) journal.clear(attempt)
      if (reason instanceof ApiError && reason.status === 401) onSessionEnded()
      setActionError(errorMessage(reason, 'The result could not be confirmed; check the original request.'))
    } finally {
      if (mutation.current === controller) {
        window.clearTimeout(actionTimer.current); mutation.current = null
        if (mounted.current && !controller.signal.aborted) { setBusy(false); void refresh(); refreshWorkspace(confirmed) }
      }
    }
  }
  function command(kind: SimulatorCommand['kind'], payload: SimulatorCommand['payload'], label: string) {
    if (!status || pending || mutation.current || factsRefreshing || staleEditor) return
    if (isReplay && (!kind.startsWith('clock.') || status.replay?.done || status.replay?.error_code)) return
    void send({ command: { request_id: crypto.randomUUID(), run_id: status.run_id, kind, payload }, label })
  }
  function replay() {
    if (!status || isReplay || pending || mutation.current || factsRefreshing || staleEditor) return
    void send({ replay: { request_id: crypto.randomUUID(), expected_run_id: status.run_id }, label: 'New replay and run switch' })
  }
  function startToday() {
    if (!status || isReplay || pending || mutation.current || factsRefreshing || staleEditor) return
    // Every reset starts from the same standard opening of the SKF workshop.
    void send({ today: { request_id: crypto.randomUUID(), expected_run_id: status.run_id, scenario_version: 'workshop-full-2' }, label: 'Reset today' })
  }
  const isReplay = Boolean(status?.replay) || currentSnapshot?.source.source_system === 'factory-simulator-replay'
  const zone = safeZone(snapshot?.profile.timezone)
  const actualDay = dayKeyOf(status?.server_time ? Date.parse(status.server_time) : wallNow, zone)
  const replayFinished = status?.replay?.done === true
  const staleEditor = Boolean(editor && (!currentSnapshot || currentSnapshot.run_id !== status?.run_id || targetFingerprint(editor.selection, currentSnapshot) === null || targetFingerprint(editor.selection, editor.snapshot) !== targetFingerprint(editor.selection, currentSnapshot)))
  const disabled = !status || busy || factsRefreshing || pending !== null || staleEditor
  function openEditor(selected: FactoryEditor) {
    if (!currentSnapshot || busy || pending) return
    setEditor({ selection: selected, snapshot: currentSnapshot, generation: 0 })
    setDirty(false); setActionError(''); setMessage('')
  }
  const feedback = <div id="factory-action-feedback" tabIndex={-1}>
    {actionError ? <p className="notice" role="alert">{actionError}</p> : null}
    {factsReadError ? <p className="notice" role="alert">{factsReadError}</p> : null}
    {message ? <div role="status"><p>{message}</p>{editor ? <p className="muted">Changes that may affect the schedule will alert the manager.</p> : null}</div> : null}
    {factsRefreshing ? <p role="status">Refreshing the shop floor.</p> : null}
    {busy ? <p role="status">Checking the result.</p> : null}
    {pending && !busy ? <div className="notice"><p>The result of “{pending.label}” is pending; new actions are blocked until it is checked.</p><Button variant="outline" type="button" onClick={() => { void send(pending) }}>Check the original request</Button></div> : null}
  </div>
  return <section className="factory-workspace" aria-label="Disruption simulator">
    <SimulatorControls status={status} disabled={busy || factsRefreshing || pending !== null || Boolean(journal.error)} isReplay={isReplay} hasPlan={Boolean(currentSnapshot?.active_plan_version)} actualDay={actualDay} onStartToday={startToday} onReplay={replay} command={command} onError={setActionError} />
    {replayFinished ? <p role="status">The replay has finished; the records are view-only.</p> : null}
    {status?.replay?.error_code ? <p className="notice" role="alert">Processing the replay records failed; check the run details and contact the administrator.</p> : null}
    {status?.replay ? <details className="detail-block"><summary>Replay run details</summary><dl className="evidence-list"><div><dt>Next record version / target version</dt><dd>{status.replay.next_revision} / {status.replay.target_revision}</dd></div>{status.replay.error_code ? <div><dt>Error code</dt><dd>{status.replay.error_code}</dd></div> : null}</dl></details> : null}
    {readError ? <p className="notice" role="alert">{readError}</p> : null}
    {journal.error ? <p className="notice" role="alert">{journal.error}</p> : null}
    {journal.canArchiveRetiredBusiness ? <Button variant="outline" className="secondary" onClick={() => { journal.archiveRetiredBusiness() }}>Keep old records and continue</Button> : null}
    {!editor ? feedback : null}
    {currentSnapshot ? <FactoryOverview key={changeVersion} changes={readChanges(`byof.changes:${factoryId}:${currentSnapshot.run_id}`)} snapshot={currentSnapshot} finishedGoods={finishedGoods} disabled={disabled || factsRefreshing || Boolean(journal.error)} onEdit={openEditor} /> : <p className="muted">The shop floor data has not loaded yet.</p>}
    {editor ? <FactoryEditorDialog label={editorLabel(editor.selection)} busy={busy} dirty={dirty} onClose={() => setEditor(null)}>
      {feedback}
      {staleEditor && !factsRefreshing ? <div className="notice" role="alert"><p>This record or its run was updated or no longer exists; your input is kept. Load the latest data before editing.</p>{currentSnapshot ? <Button variant="outline" type="button" className="secondary" disabled={busy || pending !== null} onClick={() => {
        setEditor({ selection: editor.selection, snapshot: currentSnapshot, generation: editor.generation + 1 }); setDirty(false)
      }}>{dirty ? 'Discard input and load the latest data' : 'Load the latest data'}</Button> : null}</div> : null}
      <div key={editor.generation} onChangeCapture={() => setDirty(true)}>
      {snapshot && selection ? <FactoryBusinessEditor snapshot={snapshot} selection={selection} disabled={disabled || isReplay} zone={zone} status={status} command={command} onError={setActionError} /> : null}
    </div></FactoryEditorDialog> : null}
  </section>
}
