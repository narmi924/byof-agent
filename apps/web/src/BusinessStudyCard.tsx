import { Button } from './components/ui/button'
import { Input } from './components/ui/input'
import { useState } from 'react'
import type { BusinessOption, BusinessStudyJob } from './businessContracts'
import { actionableOptions, quotedCost } from './businessContracts'
import type { Snapshot } from './contracts'
import { dateTime } from './presentation'
import { Badge } from './ui'
import { count, measureNames } from './copy'

const statusNames: Record<BusinessOption['status'], string> = { FEASIBLE: 'Feasible in trial', INFEASIBLE: 'Not feasible under current conditions', UNKNOWN: 'Not determined yet', BLOCKED: 'Conditions missing', BUDGET_EXHAUSTED: 'Calculation budget used up', CHECK_FAILED: 'Check failed' }
const priorityNames = { contribution: 'profit impact', cash: 'new cash outlay', delivery: 'delivery impact', stability: 'fewer changes', overtime: 'less overtime' }

function unavailableOption(job: BusinessStudyJob, snapshot: Snapshot): string | null {
  if (job.request.kind === 'production_exception') return null
  if (job.request.kind === 'urgent_order') {
    const productId = job.request.existing_order_id ? snapshot.orders.find(order => order.order_id === job.request.existing_order_id)?.product_id : job.request.order?.product_id
    const rule = snapshot.business_terms?.delivery_rules.find(item => item.product_id === productId)
    return productId && (!rule?.partial_delivery_allowed || rule.max_deliveries < 2) && !job.study?.options.some(option => option.kind === 'partial_delivery')
      ? 'The source has no split-delivery permission for this product, so no split-delivery option is offered this time.' : null
  }
  const now = Date.parse(snapshot.snapshot_clock)
  const availableQuote = snapshot.business_terms?.expedite_quotes.some(quote => {
    const receipt = snapshot.receipts.find(item => item.receipt_id === quote.receipt_id)
    return (!job.request.receipt_id || quote.receipt_id === job.request.receipt_id) && receipt
      && ['EXPECTED', 'CONFIRMED'].includes(receipt.status) && receipt.version === quote.receipt_version
      && Date.parse(receipt.eta) === Date.parse(quote.original_eta) && receipt.quantity === quote.quantity
      && Date.parse(quote.valid_until) > now && Date.parse(quote.expedited_eta) >= now
      && (!snapshot.horizon || Date.parse(quote.expedited_eta) < Date.parse(snapshot.horizon.end_at))
  })
  return !availableQuote && !job.study?.options.some(option => option.kind === 'receipt_expedite')
    ? 'The source has no valid expedited-receipt quote, so only the existing supply arrangement is compared this time.' : null
}

function failureMessage(code: string | null): string {
  if (code === 'WORKER_RETRY_EXHAUSTED') return 'The calculation service did not finish the comparison after several attempts. You can check the cause in this conversation and continue.'
  if (code === 'WORKER_FAILURE') return 'The calculation service could not finish this comparison. You can check the cause in this conversation and continue.'
  return 'This comparison did not finish; ask me in the conversation to find out why and continue.'
}

export function BusinessStudyCard({ job, snapshot, approvedOptionId, disabled = false, onContinue, onDiscuss, onExecute }: { job: BusinessStudyJob; snapshot: Snapshot; approvedOptionId?: string | undefined; disabled?: boolean; onContinue?: (message: string) => void; onDiscuss?: ((prefix: string) => void) | undefined; onExecute?: ((optionId: string, overtime: boolean, customer: boolean) => void) | undefined }) {
  const treatment = job.request.kind === 'production_exception'
  const options = job.study?.options ?? []
  const primary = actionableOptions(job)
  const secondary = options.filter(option => !primary.includes(option))
  const approved = options.find(option => option.option_id === approvedOptionId)
  const current = !approved && job.current && (treatment || job.study?.origin_snapshot_hash === snapshot.content_hash) && job.study?.run_id === snapshot.run_id
  const missingOption = unavailableOption(job, snapshot)
  const zone = snapshot.profile.timezone
  const topic = job.request.kind === 'urgent_order' ? `fulfilment comparison for order ${job.request.existing_order_id ?? job.request.order?.order_id ?? ''}` : treatment ? 'disruption response comparison' : 'material shortage comparison'
  const failed = job.state === 'FAILED' || job.state === 'CANCELLED'
  const followUp = failed ? `Check why this ${topic} did not finish and continue the analysis from the latest shop floor facts.` : `Check the latest shop floor facts, redo this ${topic}, and explain the existing customer commitments and open conditions.`
  return <section className="business-study" aria-label="Response option comparison">
    <header className="study-head">
      <h3>{job.request.kind === 'urgent_order' ? 'Rush order fulfilment comparison' : treatment ? 'Response options' : 'Material shortage comparison'}</h3>
      {job.study ? <span>{treatment ? `${primary.length} available · ranked by ${priorityNames[job.request.economic_priority ?? 'contribution']} · ` : ''}shop floor at {dateTime(job.study.origin_snapshot_clock, zone)}</span> : null}
    </header>
    {job.request.existing_order_id ? <p>Analyzing order {job.request.existing_order_id}; the options below may propose a new due date for this order.</p> : null}
    {job.state === 'QUEUED' ? <p role="status">The comparison is queued and updates automatically once the calculation starts.</p> : job.state === 'RUNNING' ? <p role="status">Comparing feasibility and delivery impact; results update automatically.</p> : null}
    {failed ? <p role="alert">{job.state === 'FAILED' ? failureMessage(job.error_code) : 'This comparison was cancelled and produced no new results. You can continue the analysis in this conversation.'}</p> : null}
    {job.error_code ? <details><summary>Why the comparison did not finish</summary><p>{job.error_code}</p></details> : null}
    {missingOption ? <p className="field-hint">{missingOption}</p> : null}
    {job.study ? <>
      {approved ? <p role="status">Approved “{approved.title}”; see the receipt below for progress.</p> : !current ? <p role="status">The shop floor facts have changed; these results are kept for reference. Ask the Agent in this conversation to check the latest facts and compare again.</p> : null}
      {!treatment ? <p>Advisory comparison only; ask me to compare response options when this needs handling.</p> : null}
      {treatment && job.request.max_cash_outlay_minor !== undefined && job.request.max_cash_outlay_minor !== null ? <p className="field-hint">Cash limit {quotedCost(job.request.max_cash_outlay_minor, 'SGD')}</p> : null}
      {treatment && !primary.length ? <p>No option can be executed directly this time; see the comparison results below for the reasons.</p> : null}
      <ol className="study-options">{primary.map((option, index) => <li key={option.option_id}><OptionView option={option} number={treatment ? index + 1 : null} recommended={treatment && index === 0 && !approved} approved={option === approved} snapshot={snapshot} existingOrder={job.request.existing_order_id} disabled={disabled || !current} onExecute={treatment && !approved ? onExecute : undefined} onDiscuss={treatment && current ? onDiscuss : undefined} /></li>)}</ol>
      {secondary.length ? <details className="study-other" open={primary.length === 0}><summary>Options not adopted ({secondary.length})</summary><ol className="study-options">{secondary.map(option => <li key={option.option_id}><OptionView option={option} number={null} snapshot={snapshot} existingOrder={job.request.existing_order_id} disabled /></li>)}</ol></details> : null}
    </> : null}
    {onContinue && !approved && (failed || (job.study && !current)) ? <div className="proposal-actions"><Button variant="outline" type="button" className="secondary" disabled={disabled} onClick={() => onContinue(followUp)}>{failed ? 'Continue in this conversation' : 'Re-compare with latest facts'}</Button></div> : null}
  </section>
}

function OptionView({ option, number, snapshot, existingOrder, disabled, onExecute, onDiscuss, recommended = false, approved = false }: { option: BusinessOption; number: number | null; recommended?: boolean; approved?: boolean; snapshot: Snapshot; existingOrder: string | null | undefined; disabled: boolean; onExecute?: ((optionId: string, overtime: boolean, customer: boolean) => void) | undefined; onDiscuss?: ((prefix: string) => void) | undefined }) {
  const [overtime, setOvertime] = useState(false)
  const [customer, setCustomer] = useState(false)
  const zone = snapshot.profile.timezone
  const customerRequired = option.actions?.some(action => ['order_due', 'order_quantity'].includes(action.kind)) ?? false
  const missingQuoteCost = option.quote_id !== null && option.cost_minor === null
  const existing = option.impacts.filter(i => i.existing_commitment)
  // Dates no measure could keep, proposed for the customer to agree.
  const dueChanges = option.actions?.filter(action => action.kind === 'order_due') ?? []
  const newDue = dueChanges.find(action => action.target_id === existingOrder)
  const economics = option.economics
  const executable = Boolean(onExecute) && option.status === 'FEASIBLE' && economics?.status === 'ESTIMATED' && !option.diagnostic_only
  return <article className={`study-option${recommended || approved ? ' is-recommended' : ''}`} aria-label={number ? `Option ${number}: ${option.title}` : option.title}>
    <div className="study-option-head">
      {number ? <span className="study-option-no">Option {number}</span> : null}
      {recommended ? <Badge tone="info">Recommended</Badge> : null}
      {approved ? <Badge tone="positive">Approved</Badge> : null}
      {option.status !== 'FEASIBLE' ? <Badge tone="critical">{statusNames[option.status]}</Badge> : number === null ? <Badge tone="positive">{statusNames[option.status]}</Badge> : null}
      {option.diagnostic_only ? <span className="muted">Diagnostic only</span> : null}
      <h4>{option.title}</h4>
    </div>
    <dl className="study-metrics">
      {newDue ? <div><dt>New due date</dt><dd>{dateTime(newDue.ready_at, zone)}</dd></div> : option.requested_quantity !== null ? <div><dt>On time</dt><dd>{option.on_time_quantity ?? 'Pending'} / {option.requested_quantity} pcs</dd></div> : null}
      <div><dt>Completion</dt><dd>{dateTime(option.completion_at, zone)}</dd></div>
      {economics?.status === 'ESTIMATED' ? <>
        <div><dt>New cash outlay</dt><dd>{quotedCost(economics.incremental_cash_outlay_minor, 'SGD')}</dd></div>
        <div><dt>Profit impact</dt><dd>{quotedCost(economics.net_contribution_minor, 'SGD')}</dd></div>
      </> : null}
      <div><dt>Added overtime</dt><dd>{option.incremental_overtime_minutes === null ? 'Not confirmed' : `${option.incremental_overtime_minutes} staff-min`}</dd></div>
      <div><dt>Changed operations</dt><dd>{option.changed_operations === null ? 'Not confirmed' : `${option.changed_operations} ops`}</dd></div>
      {option.quote_id ? <div><dt>Source resupply quote</dt><dd>{quotedCost(option.cost_minor, option.currency)}</dd></div> : null}
    </dl>
    {number === null || option.status !== 'FEASIBLE' ? <p>{option.summary}</p> : null}
    {option.actions?.length ? <ul className="study-measures">{option.actions.map(action => <li key={action.action_id}>
      <b>{measureNames[action.kind]}</b> {action.target_id}
      {action.quantity > 0 ? ` · ${action.quantity} ${action.kind === 'order_quantity' ? 'pcs' : snapshot.profile.materials.find(material => material.material_id === action.target_id)?.unit ?? 'units'}` : ''}{action.kind === 'order_quantity' ? '' : action.mode === 'immediate' ? ' · in stock immediately after approval' : ` · ${dateTime(action.ready_at, zone)}`}
    </li>)}</ul> : option.status === 'FEASIBLE' ? <p className="muted">No extra purchases or resource measures needed</p> : null}
    {economics && economics.status !== 'ESTIMATED' ? <p>Not enough costing data; profit impact is not reported. {economics.missing.join(' ')}</p> : null}
    {option.deliveries.length ? <ol className="business-deliveries">{option.deliveries.map((d, i) => <li key={`${i}:${d.ready_at}`}>{option.deliveries.length === 1 ? 'Full delivery' : i === 0 ? 'First delivery' : 'Later delivery'}: {d.quantity} pcs, {dateTime(d.ready_at, zone)}</li>)}</ol> : null}
    <p className="study-commitments">{dueChanges.length ? `Customer agreement needed for new due dates: ${dueChanges.map(action => `${action.target_id} from ${dateTime(option.impacts.find(i => i.order_id === action.target_id)?.requested_due_at, zone)} to ${dateTime(action.ready_at, zone)}`).join('; ')}; other orders keep their original due dates.` : option.protects_existing_commitments === true ? existingOrder ? 'Delivery commitments of other orders are protected; the due date proposal for the selected order is shown above.' : 'Existing customer commitments are protected.' : option.protects_existing_commitments === false ? existingOrder ? 'This option affects delivery commitments of other orders and needs further discussion.' : 'This option affects existing customer commitments and needs further discussion.' : existingOrder ? 'The impact on delivery commitments of other orders is not confirmed yet.' : 'The impact on existing customer commitments is not confirmed yet.'}</p>
    {option.kind === 'earliest_completion' ? <p className="field-hint">{option.earliest_completion_proven ? 'This is the earliest completion under current conditions.' : 'This completion works; an earlier one may still be possible.'}</p> : null}
    {option.kind === 'partial_delivery' ? <p className="field-hint">{option.maximum_on_time_quantity_proven ? 'This is the largest on-time quantity under current conditions.' : 'More may still be possible on time.'}</p> : null}
    {missingQuoteCost ? <p>The resupply cost is not confirmed yet; verify the quote first.</p> : null}
    {existing.length || economics || option.assumptions.length ? <details className="study-details"><summary>Order impact, cost details and calculation conditions</summary>
      {existing.length ? <><p className="field-hint">Impact on {count(existing.length, 'existing order')}</p><ul className="business-impacts">{existing.map(i => <li key={i.order_id}><strong>{i.order_id}</strong>: on time {i.on_time_quantity ?? 'not confirmed'} / {i.quantity} pcs; completion {dateTime(i.completion_at, zone)}; tardiness {i.tardiness_minutes === null ? 'not confirmed' : `${i.tardiness_minutes} min`}; versus the previous plan {i.completion_change_minutes === null ? 'not confirmed' : i.completion_change_minutes > 0 ? `${i.completion_change_minutes} min later` : i.completion_change_minutes < 0 ? `${Math.abs(i.completion_change_minutes)} min earlier` : 'unchanged'}.</li>)}</ul></> : null}
      {economics ? <section aria-label="Costs and benefits">
        <p className="field-hint">Costs and benefits · SGD · price catalog {economics.catalog_version}</p>
        {economics.status === 'ESTIMATED' ? <p>Versus the current feasible baseline: {economics.improvement_minor === null ? 'no feasible baseline, improvement not determined' : quotedCost(economics.improvement_minor, 'SGD')}</p> : null}
        <ul>{economics.lines.map((line, index) => <li key={index}>{line.label}: {quotedCost(line.amount_minor, 'SGD')}; {line.basis}</li>)}</ul>
        {economics.adverse ? <p>Adverse case: profit impact {quotedCost(economics.adverse.net_contribution_minor, 'SGD')}; new cash {quotedCost(economics.adverse.incremental_cash_outlay_minor, 'SGD')}. {economics.adverse.basis}</p> : null}
        {economics.assumptions.map(assumption => <p key={assumption}>{assumption}</p>)}
      </section> : null}
      {option.assumptions.length ? <ul className="business-impacts">{option.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul> : null}
    </details> : null}
    {executable || onDiscuss ? <div className="study-actions">
      {executable && option.allow_overtime ? <label><Input type="checkbox" checked={overtime} onChange={event => setOvertime(event.target.checked)} disabled={disabled} />I agree to the listed overtime</label> : null}
      {executable && customerRequired ? <label><Input type="checkbox" checked={customer} onChange={event => setCustomer(event.target.checked)} disabled={disabled} />The customer agreed to the listed due date or quantity change</label> : null}
      <span className="study-actions-buttons">
        {onDiscuss ? <Button variant="ghost" type="button" disabled={disabled} onClick={() => onDiscuss(`About “${option.title}”: `)}>Comment</Button> : null}
        {executable && onExecute ? <Button className="approve" type="button" disabled={disabled || (option.allow_overtime && !overtime) || (customerRequired && !customer)} onClick={() => onExecute(option.option_id, overtime, customer)}>Approve and execute</Button> : null}
      </span>
    </div> : null}
  </article>
}
