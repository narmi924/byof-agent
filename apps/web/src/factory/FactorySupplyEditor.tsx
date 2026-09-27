import { Input } from '../components/ui/input'
import { useState } from 'react'
import { factoryLocalToUtc } from '../dayWindow'
import { displayStatus } from '../presentation'
import { FactoryControlForm } from './FactoryControlForm'
import { FactoryQuoteEditor } from './FactoryQuoteEditor'
import { integer } from './formValue'
import { localTime } from './termsValue'
import type { FactoryEditorProps } from './editorTypes'

export function FactorySupplyEditor({ snapshot, selection, disabled, zone, status, command, onError }: FactoryEditorProps) {
  const [receiptAction, setReceiptAction] = useState('')
  if (selection.kind === 'quote') return <FactoryQuoteEditor snapshot={snapshot} selection={selection} disabled={disabled} zone={zone} status={status} command={command} onError={onError} />

  if (selection.kind === 'inventory') {
    const stock = snapshot.inventory.find(item => item.material_id === selection.materialId)
    if (!stock) return null
    const material = snapshot.profile.materials.find(item => item.material_id === stock.material_id)
    return <FactoryControlForm id="factory-stock-count" title="Physical stock count" button="Confirm stock count" disabled={disabled} onError={onError} onSubmit={data => {
      const counted = integer(data, 'counted', 0)
      if (counted < stock.reserved) throw new Error('The counted quantity cannot be below the reserved quantity; check WIP consumption first.')
      if (counted === stock.on_hand) throw new Error('The counted quantity has not changed.')
      const reason = String(data.get('reason'))
      if (reason === 'SCRAP' && counted > stock.on_hand) throw new Error('Scrap can only reduce stock.')
      command('inventory.reconcile', { material_id: stock.material_id, expected_version: stock.version, counted_on_hand: counted, reason }, 'Stock count')
    }}>
      <p className="muted">Enter the verified physical quantity; future receipts are recorded separately under “Inventory and supply”.</p>
      <p>Material: {material?.name ?? stock.material_id} · on hand {stock.on_hand} {stock.unit} · reserved {stock.reserved}</p>
      <label>Counted physical quantity ({stock.unit})<Input name="counted" type="number" min={stock.reserved} step="1" required /></label>
      <label>Reason for change<select name="reason"><option value="COUNT_CORRECTION">Stock count correction</option><option value="SCRAP">Scrap loss</option></select></label>
    </FactoryControlForm>
  }

  if (selection.kind === 'new-receipt') return snapshot.profile.materials.length ? <FactoryControlForm id="factory-receipt-add" title="Record a confirmed receipt" button="Confirm receipt" disabled={disabled} onError={onError} onSubmit={data => {
    const receiptId = String(data.get('receipt_id') ?? '').trim()
    const materialId = String(data.get('material'))
    if (!/^[^\s]{1,160}$/.test(receiptId) || snapshot.receipts.some(item => item.receipt_id === receiptId)) throw new Error('Enter an unused receipt ID.')
    if (!snapshot.profile.materials.some(item => item.material_id === materialId)) throw new Error('Choose a material of this factory.')
    const eta = factoryLocalToUtc(String(data.get('eta') ?? ''), zone)
    if (Date.parse(eta) <= Date.parse(status?.business_clock ?? '')) throw new Error('The expected arrival must be later than the current factory time.')
    command('receipt.add', { receipt_id: receiptId, material_id: materialId, quantity: integer(data, 'quantity', 1), eta }, 'Record confirmed receipt')
  }}>
    <p className="muted">Only record future receipts confirmed by the supplier; an expected receipt is not current stock.</p>
    <label>Receipt ID<Input name="receipt_id" maxLength={160} required /></label>
    <label>Material<select name="material" defaultValue={selection.materialId}>{snapshot.profile.materials.map(item => <option key={item.material_id} value={item.material_id}>{item.name} · {item.unit}</option>)}</select></label>
    <label>Confirmed quantity<Input name="quantity" type="number" min="1" step="1" required /></label>
    <label>Expected arrival ({zone})<Input name="eta" type="datetime-local" step="60" required /></label>
  </FactoryControlForm> : null

  if (selection.kind !== 'receipt') return null
  const receipt = snapshot.receipts.find(item => item.receipt_id === selection.receiptId && ['EXPECTED', 'CONFIRMED'].includes(item.status))
  if (!receipt) return null
  return <FactoryControlForm id="factory-receipt-change" title="Update receipt" button="Confirm receipt change" disabled={disabled} onError={onError} onSubmit={data => {
    if (!receiptAction) throw new Error('Choose the receipt change.')
    if (receiptAction === 'receive') command('receipt.receive', { receipt_id: receipt.receipt_id }, 'Goods received')
    else if (receiptAction === 'delay') {
      const eta = factoryLocalToUtc(String(data.get('eta') ?? ''), zone)
      if (Date.parse(eta) <= Date.parse(receipt.eta) || Date.parse(eta) <= Date.parse(status?.business_clock ?? '')) throw new Error('The new expected arrival must be later than the original time and the current factory time.')
      command('receipt.delay', { receipt_id: receipt.receipt_id, eta }, 'Receipt delayed')
    } else if (receiptAction === 'shortfall') {
      const quantity = integer(data, 'quantity', 1)
      if (quantity >= receipt.quantity) throw new Error('The new expected quantity must be less than the original quantity.')
      command('receipt.shortfall', { receipt_id: receipt.receipt_id, quantity }, 'Short delivery')
    } else command('receipt.cancel', { receipt_id: receipt.receipt_id }, 'Cancel expected receipt')
  }}>
    <p>Receipt: {receipt.receipt_id} · {receipt.material_id} · planned {receipt.quantity} {displayStatus(receipt.unit)} · expected {localTime(receipt.eta, zone).replace('T', ' ')}</p>
    <label>Receipt change<select name="action" value={receiptAction} onChange={event => setReceiptAction(event.target.value)} required><option value="">Choose a change</option><option value="shortfall">Short delivery</option><option value="cancel">Cancel expected receipt</option><option value="delay">Delay expected arrival</option><option value="receive">Confirm goods received</option></select></label>
    {receiptAction === 'delay' ? <label>New expected arrival ({zone})<Input name="eta" type="datetime-local" step="60" defaultValue={localTime(receipt.eta, zone)} required /></label> : null}
    {receiptAction === 'shortfall' ? <label>New expected quantity<Input name="quantity" type="number" min="1" step="1" required /></label> : null}
    {receiptAction === 'cancel' ? <label className="checkbox-label"><Input type="checkbox" required />Confirm cancelling this outstanding supply</label> : null}

  </FactoryControlForm>
}
