import { Input } from '../components/ui/input'
import { factoryLocalToUtc } from '../dayWindow'
import { useState } from 'react'
import { displayStatus } from '../presentation'
import { FactoryControlForm } from './FactoryControlForm'
import { FactoryDeliveryRuleEditor } from './FactoryDeliveryRuleEditor'
import { integer } from './formValue'
import type { FactoryEditorProps } from './editorTypes'

function localTime(value: string, zone: string): string {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(new Date(value)).replace(' ', 'T')
}

export function FactoryOrdersEditor({ snapshot, selection, disabled, zone, status, command, onError }: FactoryEditorProps) {
  const [cancelling, setCancelling] = useState(false)
  if (selection.kind === 'delivery-rule') return <FactoryDeliveryRuleEditor snapshot={snapshot} selection={selection} disabled={disabled} zone={zone} status={status} command={command} onError={onError} />

  if (selection.kind === 'new-order') return snapshot.profile.products.length ? <FactoryControlForm id="factory-order-add" title="New order" button="Confirm new order" disabled={disabled} onError={onError} onSubmit={data => {
    const productId = String(data.get('product'))
    const product = snapshot.profile.products.find(item => item.product_id === productId)
    const quantity = integer(data, 'quantity', 1)
    if (!product || quantity % product.batch_size !== 0) throw new Error(`The order quantity must be a whole multiple of the product batch size${product ? ` ${product.batch_size}` : ''}.`)
    const orderId = String(data.get('order_id') ?? '').trim()
    if (!/^[^\s]{1,160}$/.test(orderId)) throw new Error('The order ID cannot be empty or contain spaces, and has at most 160 characters.')
    if (snapshot.orders.some(item => item.order_id === orderId)) throw new Error('The order ID already exists; enter a new ID.')
    command('order.add', { order_id: orderId, product_id: productId, quantity, due_at: factoryLocalToUtc(String(data.get('due') ?? ''), zone), priority_weight: integer(data, 'priority', 1), hard_deadline: data.get('hard') === 'on', version: 1, split_revision: 1, status: 'CONFIRMED' }, 'New order')
  }}>

    <label>Order ID<Input name="order_id" maxLength={160} required /></label>
    <label>Product and batch size<select name="product">{snapshot.profile.products.map(item => <option key={item.product_id} value={item.product_id}>{item.name} · {item.batch_size} pcs per batch</option>)}</select></label>
    <label>Order quantity (pcs)<Input name="quantity" type="number" min="1" step="1" required /></label>
    <label>Due ({zone})<Input name="due" type="datetime-local" step="60" required /></label>
    <label>Due-date priority weight<Input name="priority" type="number" min="1" step="1" defaultValue="1" required /></label>
    <label className="checkbox-label"><Input name="hard" type="checkbox" />Customer does not allow delay</label>
  </FactoryControlForm> : null

  if (selection.kind !== 'order') return null
  const order = snapshot.orders.find(item => item.order_id === selection.orderId)
  const product = snapshot.profile.products.find(item => item.product_id === order?.product_id)
  if (!order || !product || !['CONFIRMED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'].includes(order.status)) return null

  return <FactoryControlForm id="factory-order-revise" title="Change order" button="Confirm order change" disabled={disabled} onError={onError} onSubmit={data => {
    const quantity = integer(data, 'quantity', 0)
    if (quantity === 0 && order.quantity > 0 && data.get('confirm_cancel') !== 'on') throw new Error('Confirm the consequences of cancelling the order before submitting.')
    if (quantity % product.batch_size !== 0) throw new Error(`The quantity must be a whole multiple of ${product.batch_size} pcs per batch.`)
    const dueAt = factoryLocalToUtc(String(data.get('due') ?? ''), zone)
    const priority = integer(data, 'priority', 1)
    const hard = data.get('hard') === 'on'
    if (quantity === order.quantity && Date.parse(dueAt) === Date.parse(order.due_at) && priority === order.priority_weight && hard === order.hard_deadline) throw new Error('The order has not changed.')
    command('order.revise', { order_id: order.order_id, expected_version: order.version, quantity, due_at: dueAt, priority_weight: priority, hard_deadline: hard }, 'Order change')
  }}>
    <p className="muted">A quantity of 0 cancels the order; started batches still finish and qualified surplus goes to stock. Enter a positive number to restore a cancelled order.</p>
    <p>Order: {order.order_id} · {order.status === 'COMPLETED' ? 'Production completed' : order.status === 'IN_PROGRESS' ? 'In production' : displayStatus(order.status)} · {order.quantity} pcs</p>
    <label>Demand quantity ({product.batch_size} pcs per batch)<Input name="quantity" type="number" min="0" step={product.batch_size} defaultValue={order.quantity} onChange={event => setCancelling(event.target.value === '0' && order.quantity > 0)} required /></label>
    {cancelling && <label className="checkbox-label"><Input name="confirm_cancel" type="checkbox" required />Confirm cancelling {order.quantity} pcs of demand for {order.order_id}; started batches still finish and qualified surplus goes to stock</label>}
    <label>Due ({zone})<Input name="due" type="datetime-local" step="60" defaultValue={localTime(order.due_at, zone)} required /></label>
    <label>Due-date priority weight<Input name="priority" type="number" min="1" step="1" defaultValue={order.priority_weight} required /></label>
    <label className="checkbox-label"><Input name="hard" type="checkbox" defaultChecked={order.hard_deadline} />Customer does not allow delay</label>
  </FactoryControlForm>
}
