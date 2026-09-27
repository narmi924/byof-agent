import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { useState } from 'react'
import type { ExpediteQuote } from '../businessContracts'
import type { Snapshot } from '../contracts'
import type { FactoryEditor } from '../factoryEditor'
import { factoryLocalToUtc, safeZone } from '../dayWindow'
import { dateTime } from '../presentation'
import type { FactoryEditorProps } from './editorTypes'
import { FactoryControlForm } from './FactoryControlForm'
import { identifier, localTime, readText } from './termsValue'

type Props = FactoryEditorProps & { selection: Extract<FactoryEditor, { kind: 'quote' }> }

function quoteProblem(quote: ExpediteQuote, snapshot: Snapshot): string | null {
  const receipt = snapshot.receipts.find(item => item.receipt_id === quote.receipt_id)
  if (!receipt || !['EXPECTED', 'CONFIRMED'].includes(receipt.status)) return 'The receipt was cancelled, received or no longer exists and cannot be used for new comparisons.'
  if (receipt.version !== quote.receipt_version || Date.parse(receipt.eta) !== Date.parse(quote.original_eta) || receipt.quantity !== quote.quantity) return 'The receipt quantity, date or version has changed; verify the quote again.'
  if (Date.parse(quote.valid_until) <= Date.parse(snapshot.snapshot_clock)) return 'The quote has expired and cannot be used for new comparisons.'
  if (Date.parse(quote.expedited_eta) < Date.parse(snapshot.snapshot_clock)) return 'The expedited arrival in the quote has passed; verify it again.'
  return null
}

function QuoteForm({ snapshot, disabled, command, onError, quote, receiptId }: Omit<Props, 'selection'> & { quote: ExpediteQuote | undefined; receiptId: string }) {
  const [unknownCost, setUnknownCost] = useState(quote?.cost_minor === null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const receipt = snapshot.receipts.find(item => item.receipt_id === receiptId)
  const pendingReceipt = receipt && ['EXPECTED', 'CONFIRMED'].includes(receipt.status)
  const terms = snapshot.business_terms, zone = safeZone(snapshot.profile.timezone)
  const enterprise = terms?.evidence_mode === 'enterprise'
  return <FactoryControlForm id="factory-expedite-quote-editor" title="Supplier expedite quote" disabled={disabled || enterprise} onError={onError} onSubmit={data => {
    if (!receipt || !pendingReceipt) throw new Error('This receipt is no longer awaiting arrival; a new quote cannot be entered.')
    const eta = factoryLocalToUtc(readText(data, 'eta'), zone)
    const valid = factoryLocalToUtc(readText(data, 'valid'), zone)
    const now = Date.parse(snapshot.snapshot_clock)
    if (Date.parse(eta) <= now || Date.parse(eta) >= Date.parse(receipt.eta)) throw new Error('The expedited arrival must be after the current factory time and before the original expected arrival.')
    if (snapshot.horizon && Date.parse(eta) >= Date.parse(snapshot.horizon.end_at)) throw new Error('The expedited arrival must be before the end of the current scheduling window.')
    if (Date.parse(valid) <= now) throw new Error('The quote must be valid past the current factory time.')
    let cost: number | null = null, currency: string | null = null
    if (!unknownCost) {
      const raw = readText(data, 'amount')
      if (!/^\d+(\.\d{1,2})?$/.test(raw)) throw new Error('Enter a non-negative amount with at most two decimals; tick the box for an unknown cost.')
      const [units = '', fraction = ''] = raw.split('.')
      cost = Number(units) * 100 + Number(fraction.padEnd(2, '0'))
      currency = readText(data, 'currency')
      if (!Number.isSafeInteger(cost) || !['CNY', 'USD', 'EUR'].includes(currency)) throw new Error('Check the amount and choose a supported currency.')
    }
    const quoteId = quote?.quote_id ?? identifier(data, 'quote_id')
    command('expedite_quote.set', { expected_terms_version: terms?.version ?? null, quote_id: quoteId, receipt_id: receiptId, expected_receipt_version: receipt.version, expedited_eta: eta, valid_until: valid, source_reference: identifier(data, 'reference'), cost_minor: cost, currency }, `Save expedite quote ${quoteId}`)
  }}>
    <p className="muted">Saving a quote does not purchase or receive anything.</p>
    <p className="muted">Current factory time: {dateTime(snapshot.snapshot_clock, zone)}.</p>
    {enterprise ? <p className="notice">This quote comes from the enterprise source; maintain it in the enterprise system. It cannot be overwritten here.</p> : null}
    {!pendingReceipt ? <p className="notice">This receipt is no longer awaiting arrival, so a new quote cannot be saved; existing quotes can still be deleted.</p> : null}
    <label>Quote ID<Input name="quote_id" defaultValue={quote?.quote_id ?? ''} readOnly={Boolean(quote)} maxLength={160} required /></label>
    <p>Receipt: {receiptId}{receipt ? ` · ${receipt.quantity} ${receipt.unit}` : ' · record no longer exists'}</p>
    {receipt ? <p className="muted">Current receipt version {receipt.version}; original expected arrival {dateTime(receipt.eta, zone)}.</p> : null}
    <label>Expedited expected arrival ({zone})<Input name="eta" type="datetime-local" step="60" defaultValue={localTime(quote?.expedited_eta, zone)} required /></label>
    <label>Quote valid until ({zone})<Input name="valid" type="datetime-local" step="60" defaultValue={localTime(quote?.valid_until, zone)} required /></label>
    <label>Source reference or supplier quote ID<Input name="reference" defaultValue={quote?.source_reference ?? ''} maxLength={160} required /></label>
    <label className="checkbox-label"><Input type="checkbox" checked={unknownCost} onChange={event => setUnknownCost(event.target.checked)} />Quote amount not confirmed yet</label>
    <label>Extra cost (major currency unit)<Input name="amount" type="number" min="0" step="0.01" defaultValue={quote?.cost_minor != null ? (quote.cost_minor / 100).toFixed(2) : ''} disabled={unknownCost} required={!unknownCost} /></label>
    <label>Currency<select name="currency" defaultValue={quote?.currency ?? ''} disabled={unknownCost} required={!unknownCost}><option value="">Choose a currency</option><option value="CNY">CNY Chinese yuan</option><option value="USD">USD US dollar</option><option value="EUR">EUR Euro</option></select></label>
    <Button variant="outline" type="submit" disabled={!pendingReceipt}>Save expedite quote</Button>
    {quote ? confirmDelete ? <div role="alert"><p>After deleting quote {quote.quote_id}, later comparisons can no longer use it; the receipt record is kept.</p><Button variant="outline" type="button" className="secondary" onClick={() => command('expedite_quote.remove', { expected_terms_version: terms?.version ?? null, quote_id: quote.quote_id }, `Delete expedite quote ${quote.quote_id}`)}>Confirm delete</Button><Button variant="outline" type="button" className="ghost" onClick={() => setConfirmDelete(false)}>Keep quote</Button></div> : <Button variant="outline" type="button" className="secondary" onClick={() => setConfirmDelete(true)}>Delete this quote</Button> : null}
  </FactoryControlForm>
}

export function FactoryQuoteEditor({ selection, ...props }: Props) {
  const quote = props.snapshot.business_terms?.expedite_quotes.find(item => item.quote_id === selection.quoteId)
  if (selection.quoteId && (!quote || quote.receipt_id !== selection.receiptId)) return <p className="notice" role="alert">The selected quote no longer exists or does not belong to this receipt; close and check the latest shop floor.</p>
  const problem = quote ? quoteProblem(quote, props.snapshot) : null
  return <section className="business-facts-group" aria-label="Expedite quote facts">
    {quote ? <p className={problem ? 'notice' : 'muted'}>{problem ?? 'The quote still matches the current receipt.'} Receipt version {quote.receipt_version}, valid until {dateTime(quote.valid_until, props.snapshot.profile.timezone)}.</p> : <p className="muted">Add an expedite quote for this receipt; the cost is never assumed to be zero.</p>}
    <QuoteForm {...props} receiptId={selection.receiptId} quote={quote} />
  </section>
}
