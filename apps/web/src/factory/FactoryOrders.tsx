import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { useState } from 'react'
import type { FactorySectionProps } from './sectionProps'
import { dateTime, displayStatus } from '../presentation'

export function FactoryOrders({ snapshot, onEdit, disabled }: FactorySectionProps) {
  const [query, setQuery] = useState('')
  const zone = snapshot.profile.timezone
  const product = (id: string) => snapshot.profile.products.find(item => item.product_id === id)?.name ?? id
  const changedBatches = snapshot.production_batches?.filter(item => item.purpose !== 'CUSTOMER')
  const orders = snapshot.orders.filter(item => `${item.order_id} ${item.product_id} ${product(item.product_id)}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))

  return <div className="factory-module-content">
    <section className="factory-overview-section" aria-label="Customer orders">
      <div className="factory-overview-section-heading">
        <div><h3>Customer orders</h3></div>
        <Button variant="outline" type="button" disabled={disabled || !snapshot.profile.products.length} onClick={() => onEdit({ kind: 'new-order' })}>New order</Button>
      </div>
      {snapshot.orders.length ? <div className="factory-overview-search"><label htmlFor="factory-order-search">Search orders or products</label><Input id="factory-order-search" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Order ID or product" /><span>{orders.length} / {snapshot.orders.length}</span></div> : null}
      {orders.length ? <div className="factory-overview-table"><table>
        <caption>Current orders · {orders.length}</caption>
        <thead><tr><th>Order</th><th>Product</th><th>Demand</th><th>Due</th><th>Status</th><th>Action</th></tr></thead>
        <tbody>{orders.map(order => {
          const changed = changedBatches?.filter(batch => batch.order_id === order.order_id) ?? []
          const stock = changed.filter(batch => batch.purpose === 'STOCK').reduce((total, batch) => total + batch.quantity, 0)
          const cancelled = changed.filter(batch => batch.purpose === 'CANCELLED').reduce((total, batch) => total + batch.quantity, 0)
          return <tr key={order.order_id}>
            <th scope="row">{order.order_id}</th>
            <td>{product(order.product_id)}<span className="cell-note">{order.product_id}</span></td>
            <td>{order.quantity} pcs{stock || cancelled ? <span className="cell-note">{[stock ? `${stock} pcs to stock` : '', cancelled ? `${cancelled} pcs of unstarted work cancelled` : ''].filter(Boolean).join('; ')}</span> : null}</td>
            <td>{dateTime(order.due_at, zone)}{order.hard_deadline ? <span className="cell-note">No delay allowed</span> : null}</td>
            <td>{order.status === 'COMPLETED' ? 'Production completed' : displayStatus(order.status)}</td>
            <td><Button variant="outline" type="button" className="secondary" disabled={disabled || !['CONFIRMED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'].includes(order.status)} aria-label={`Change order ${order.order_id}`} onClick={() => onEdit({ kind: 'order', orderId: order.order_id })}>Change order</Button></td>
          </tr>
        })}</tbody>
      </table></div> : <p className="factory-overview-empty">{query && snapshot.orders.length ? 'No matching orders.' : 'No orders.'}</p>}
    </section>

    <section className="factory-overview-section" aria-label="Product delivery rules">
      <div className="factory-overview-section-heading"><div><h3>Product delivery rules</h3></div></div>
      {snapshot.profile.products.length ? <div className="factory-record-list">{snapshot.profile.products.map(item => {
        const rule = snapshot.business_terms?.delivery_rules.find(row => row.product_id === item.product_id)
        return <article key={item.product_id} className="factory-record">
          <div><h4>{item.name}</h4><p>{item.product_id} · {item.batch_size} pcs per batch</p><p>{rule ? rule.partial_delivery_allowed ? `Split delivery allowed · first delivery at least ${rule.minimum_partial_quantity} pcs · at most ${rule.max_deliveries} deliveries` : 'Split delivery not allowed' : 'No split-delivery permission recorded'}</p></div>
          <Button variant="outline" type="button" className="secondary" disabled={disabled} aria-label={`Split-delivery rule ${item.product_id}`} onClick={() => onEdit({ kind: 'delivery-rule', productId: item.product_id })}>Delivery rule</Button>
        </article>
      })}</div> : <p className="factory-overview-empty">No product data, so orders cannot be added and split-delivery rules cannot be maintained.</p>}
      {changedBatches?.length ? <details className="factory-overview-details"><summary>Where batches went after demand changes · {changedBatches.length}</summary>
        <div className="factory-overview-table"><table><caption>Source batch records</caption><thead><tr><th>Batch</th><th>Order / product</th><th>Quantity</th><th>Outcome</th></tr></thead><tbody>{changedBatches.map(batch => <tr key={batch.batch_id}><th scope="row">{batch.batch_id}</th><td>{batch.order_id} · {product(batch.product_id)}</td><td>{batch.quantity} pcs</td><td>{batch.purpose === 'STOCK' ? 'To stock; see finished goods for quality' : 'Unstarted work cancelled'}</td></tr>)}</tbody></table></div>
      </details> : null}
    </section>
  </div>
}
