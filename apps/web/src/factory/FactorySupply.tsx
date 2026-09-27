import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { useState } from 'react'
import type { ExpediteQuote } from '../businessContracts'
import { quotedCost } from '../businessContracts'
import type { FinishedGoodsLot, Snapshot } from '../contracts'
import { dateTime, displayStatus } from '../presentation'
import type { FactorySectionProps } from './sectionProps'

function quoteState(quote: ExpediteQuote, snapshot: Snapshot): string {
  const receipt = snapshot.receipts.find(item => item.receipt_id === quote.receipt_id)
  if (!receipt || !['EXPECTED', 'CONFIRMED'].includes(receipt.status)) return 'Receipt closed'
  if (receipt.version !== quote.receipt_version || receipt.quantity !== quote.quantity || Date.parse(receipt.eta) !== Date.parse(quote.original_eta)) return 'Receipt changed; verify again'
  if (Date.parse(quote.valid_until) <= Date.parse(snapshot.snapshot_clock) || Date.parse(quote.expedited_eta) < Date.parse(snapshot.snapshot_clock)) return 'Quote expired'
  return 'Not purchased'
}

export function FactorySupply({ snapshot, finishedGoods, onEdit, disabled }: FactorySectionProps & { finishedGoods?: FinishedGoodsLot[] | null | undefined }) {
  const [query, setQuery] = useState('')
  const zone = snapshot.profile.timezone
  const material = (id: string) => snapshot.profile.materials.find(item => item.material_id === id)?.name ?? id
  const product = (id: string) => snapshot.profile.products.find(item => item.product_id === id)?.name ?? id
  const stocks = snapshot.inventory.filter(item => `${item.material_id} ${material(item.material_id)}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
  const quotes = snapshot.business_terms?.expedite_quotes ?? []
  const quoteList = (items: ExpediteQuote[]) => <ul className="factory-overview-quotes">{items.map(quote => <li key={quote.quote_id}>
    <span><strong>{quote.quote_id}</strong> · receipt {quote.receipt_id} · {quotedCost(quote.cost_minor, quote.currency)} · expedited to {dateTime(quote.expedited_eta, zone)}<span className="cell-note">{quoteState(quote, snapshot)} · reference {quote.source_reference}</span></span>
    <Button variant="outline" type="button" className="ghost" disabled={disabled} aria-label={`Edit quote ${quote.quote_id}`} onClick={() => onEdit({ kind: 'quote', receiptId: quote.receipt_id, quoteId: quote.quote_id })}>Edit quote</Button>
  </li>)}</ul>

  return <div className="factory-module-content">
    <section className="factory-overview-section" aria-label="Raw materials">
      <div className="factory-overview-section-heading"><div><h3>Raw materials</h3><p>On hand includes reserved material; expected receipts are not counted as stock.</p></div></div>
      <div className="factory-overview-search"><label htmlFor="factory-material-search">Search material name or ID</label><Input id="factory-material-search" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search materials" /><span>{stocks.length} / {snapshot.inventory.length}</span></div>
      {stocks.length ? <div className="factory-overview-table"><table><caption>Current raw materials</caption><thead><tr><th>Material</th><th>On hand</th><th>Reserved</th><th>Available</th><th>Action</th></tr></thead><tbody>{stocks.map(item => <tr key={item.material_id}>
        <th scope="row">{material(item.material_id)}<span className="cell-note">{item.material_id}</span></th>
        <td>{item.on_hand} {displayStatus(item.unit)}</td><td>{item.reserved} {displayStatus(item.unit)}</td><td>{item.on_hand - item.reserved} {displayStatus(item.unit)}</td>
        <td><Button variant="outline" type="button" className="secondary" disabled={disabled} aria-label={`Count ${item.material_id}`} onClick={() => onEdit({ kind: 'inventory', materialId: item.material_id })}>Count stock</Button></td>
      </tr>)}</tbody></table></div> : <p className="factory-overview-empty">{query ? 'No matching materials.' : 'No raw material records.'}</p>}
    </section>

    <section className="factory-overview-section" aria-label="Inbound receipts">
      <div className="factory-overview-section-heading"><div><h3>Inbound receipts</h3></div><Button variant="outline" type="button" disabled={disabled || !snapshot.profile.materials.length} onClick={() => onEdit({ kind: 'new-receipt' })}>Record receipt</Button></div>
      {snapshot.receipts.length ? <div className="factory-overview-table"><table className="factory-overview-receipts"><caption>Receipts · {snapshot.receipts.length}</caption><thead><tr><th>Receipt / material</th><th>Quantity</th><th>Expected / actual</th><th>Status</th><th>Action</th></tr></thead><tbody>{snapshot.receipts.map(receipt => {
        const pending = ['EXPECTED', 'CONFIRMED'].includes(receipt.status)
        return <tr key={receipt.receipt_id}>
          <th scope="row">{receipt.receipt_id}<span className="cell-note">{material(receipt.material_id)} · {receipt.material_id}</span></th>
          <td>{receipt.quantity} {displayStatus(receipt.unit)}</td>
          <td>{dateTime(receipt.eta, zone)}<span className="cell-note">Actual: {receipt.received_at ? dateTime(receipt.received_at, zone) : 'not received'}</span></td>
          <td>{displayStatus(receipt.status)}</td>
          <td><div className="factory-overview-actions">{pending ? <><Button variant="outline" type="button" className="secondary" disabled={disabled} aria-label={`Update receipt ${receipt.receipt_id}`} onClick={() => onEdit({ kind: 'receipt', receiptId: receipt.receipt_id })}>Update receipt</Button><Button variant="outline" type="button" className="ghost" disabled={disabled} aria-label={`New quote ${receipt.receipt_id}`} onClick={() => onEdit({ kind: 'quote', receiptId: receipt.receipt_id })}>Add expedite quote</Button></> : '—'}</div></td>
        </tr>
      })}</tbody></table></div> : <p className="factory-overview-empty">No receipts.</p>}
    </section>

    <section className="factory-overview-section" aria-label="Supplier expedite quotes">
      <div className="factory-overview-section-heading"><div><h3>Supplier expedite quotes</h3><p>Quotes are only a basis for comparison; nothing has been purchased and expected arrivals are unchanged.</p></div></div>
      {quotes.length ? quoteList(quotes) : <p className="factory-overview-empty">No expedite quotes.</p>}
    </section>

    <section className="factory-overview-section" aria-label="Qualified finished goods">
      <div className="factory-overview-section-heading"><div><h3>Qualified finished goods</h3></div></div>
      {finishedGoods == null ? <p className="factory-overview-empty">The source has not reported qualified finished goods; they cannot be treated as zero.</p> : finishedGoods.length ? <div className="factory-overview-table"><table><caption>Source-verified qualified surplus · {finishedGoods.reduce((total, lot) => total + lot.quantity, 0)} pcs</caption><thead><tr><th>Batch</th><th>Product</th><th>Quantity</th><th>Completed</th></tr></thead><tbody>{finishedGoods.map(lot => <tr key={lot.batch_id}><th scope="row">{lot.batch_id}</th><td>{product(lot.product_id)}</td><td>{lot.quantity} pcs</td><td>{dateTime(lot.completed_at, zone)}</td></tr>)}</tbody></table></div> : <p className="factory-overview-empty">No source-verified qualified surplus.</p>}
    </section>
  </div>
}
