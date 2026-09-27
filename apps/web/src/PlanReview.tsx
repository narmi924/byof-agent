import type { PlanReview as Review } from './contracts'
import { dateTime } from './presentation'

export function PlanReview({ review, zone }: { review: Review; zone: string }) {
  return <section aria-label="Order impact of this approval">
    <h4>Order impact of this approval</h4>
    <p className="field-hint">Calculated as of {dateTime(review.as_of, zone)}. These are plan forecasts; only the qualified completed quantity is an actual result.</p>
    <div className="factory-overview-table"><table><thead><tr><th>Order</th><th>Demand / qualified done</th><th>Previously covered → this plan</th><th>Previous forecast completion → this plan</th><th>Promised due / on time in this plan</th></tr></thead>
      <tbody>{review.orders.map(order => <tr key={order.order_id}>
        <th scope="row">{order.order_id}{order.status === 'CANCELLED' ? ' · Cancelled' : ''}</th>
        <td>{order.quantity} / {order.qualified_completed_quantity} pcs</td>
        <td>{order.previous_covered_quantity ?? 0} → {order.plan_covered_quantity} pcs</td>
        <td>{dateTime(order.previous_completion_at, zone)} → {dateTime(order.planned_completion_at, zone)}</td>
        <td>{dateTime(order.due_at, zone)} · {order.planned_on_time_quantity} pcs</td>
      </tr>)}</tbody></table></div>
    {review.overtime.length ? <details><summary>Overtime staff and slots that need approval ({review.overtime.length})</summary><ul>{review.overtime.map((slot, index) => <li key={index}>{slot.worker_id} · {slot.resource_id}: {dateTime(slot.start_at, zone)} — {dateTime(slot.end_at, zone)}, {slot.minutes} min</li>)}</ul></details> : <p>This plan has no overtime slots to run.</p>}
    <p className="field-hint">Accept before: {dateTime(review.accept_before, zone)}. Approval re-checks the shop floor; material, staff or machine changes may require a recalculation.</p>
  </section>
}
