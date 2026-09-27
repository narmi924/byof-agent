"""Order-level facts for people and models; intermediate output is never finished goods."""

from datetime import datetime
from zoneinfo import ZoneInfo

from packages.domain.models import Candidate, Snapshot, batch_operations


def material_shortfalls(snapshot: Snapshot) -> list[dict]:
    batches, _ = batch_operations(snapshot)
    started = {a.batch_id for a in snapshot.actuals if a.actual_start is not None}
    materials = {m.material_id: m for m in snapshot.profile.materials}
    result = []
    for stock in snapshot.inventory:
        demand = sum(
            bom.quantity_per_unit * batch.quantity
            for batch in batches
            if batch.batch_id not in started
            for bom in snapshot.profile.bom
            if bom.product_id == batch.product_id and bom.material_id == stock.material_id
        )
        free = stock.on_hand - stock.reserved
        inbound = sum(
            r.quantity
            for r in snapshot.receipts
            if r.material_id == stock.material_id
            and r.status == "CONFIRMED"
            and r.eta <= snapshot.horizon.end_at
        )
        if demand > free + inbound:
            result.append(
                {
                    "material_id": stock.material_id,
                    "material_name": materials[stock.material_id].name,
                    "unit": stock.unit,
                    "unstarted_demand": demand,
                    "unreserved_on_hand": free,
                    "confirmed_inbound": inbound,
                    "minimum_shortfall": demand - free - inbound,
                }
            )
    return result


def local_time(value: datetime | None, snapshot: Snapshot) -> str | None:
    return value.astimezone(ZoneInfo(snapshot.profile.timezone)).isoformat() if value else None


def order_facts(snapshot: Snapshot, plan: Candidate | None = None) -> list[dict]:
    """Historical plan coverage is not a new feasibility or customer-delivery promise."""
    batches, operations = batch_operations(snapshot)
    purposes = {b.batch_id: b.purpose for b in snapshot.production_batches or ()}
    grouped: dict[str, list[str]] = {}
    for operation in operations:
        grouped.setdefault(operation.batch_id, []).append(operation.operation_id)
    actuals = {a.operation_id: a for a in snapshot.actuals}
    assigned = {a.operation_id: a for a in plan.assignments} if plan else {}
    gaps = {g["material_id"] for g in material_shortfalls(snapshot)}
    products = {p.product_id: p for p in snapshot.profile.products}
    today = snapshot.snapshot_clock.astimezone(ZoneInfo(snapshot.profile.timezone)).date()
    result = []
    for order in snapshot.orders:
        customer = [
            b
            for b in batches
            if b.order_id == order.order_id and purposes.get(b.batch_id, "CUSTOMER") == "CUSTOMER"
        ]
        completed = started = covered = today_quantity = on_time = 0
        ends = []
        execution_uncertain = False
        for batch in customer:
            ids = grouped.get(batch.batch_id, [])
            records = [actuals.get(identity) for identity in ids]
            qualified = bool(ids) and all(
                a and a.state == "COMPLETED" and a.quality_state == "PASSED" for a in records
            )
            if qualified:
                completed += batch.quantity
            if any(a and a.actual_start is not None for a in records):
                started += batch.quantity
            times = []
            for identity in ids:
                actual, assignment = actuals.get(identity), assigned.get(identity)
                if actual and actual.state == "COMPLETED" and actual.actual_end:
                    times.append(actual.actual_end)
                    if actual.quality_state != "PASSED":
                        execution_uncertain = True
                elif assignment:
                    times.append(assignment.end_at)
                    if assignment.end_at <= snapshot.snapshot_clock or (
                        actual and actual.state == "BLOCKED"
                    ):
                        execution_uncertain = True
            if ids and len(times) == len(ids):
                end = max(times)
                covered += batch.quantity
                ends.append(end)
                if end.astimezone(ZoneInfo(snapshot.profile.timezone)).date() <= today:
                    today_quantity += batch.quantity
                if end <= order.due_at:
                    on_time += batch.quantity
        bom = [b for b in snapshot.profile.bom if b.product_id == order.product_id]
        bound = max(0, order.quantity - started)
        for item in bom:
            stock = next((s for s in snapshot.inventory if s.material_id == item.material_id), None)
            supply = (stock.on_hand - stock.reserved if stock else 0) + sum(
                r.quantity
                for r in snapshot.receipts
                if r.material_id == item.material_id
                and r.status == "CONFIRMED"
                and r.eta <= snapshot.horizon.end_at
            )
            bound = min(bound, supply // item.quantity_per_unit)
        size = products[order.product_id].batch_size
        direct = sorted({b.material_id for b in bom} & gaps)
        # A total shortage does not prove that each sharing order must be late.
        result.append(
            {
                "order_id": order.order_id,
                "product_id": order.product_id,
                "status": order.status,
                "quantity": order.quantity,
                "due_at": local_time(order.due_at, snapshot),
                "hard_deadline": order.hard_deadline,
                "qualified_completed_quantity": completed,
                "in_progress_quantity": max(0, started - completed),
                "plan_covered_quantity": covered,
                "uncovered_quantity": max(0, order.quantity - covered),
                "planned_completion_at": local_time(max(ends), snapshot)
                if ends and covered == order.quantity
                else None,
                "planned_ready_today_quantity": today_quantity,
                "planned_on_time_quantity": on_time,
                "direct_shortage_materials": direct,
                "material_quantity_upper_bound": min(
                    order.quantity, started + bound // size * size
                ),
                "forecast_requires_revalidation": bool(
                    direct or execution_uncertain or covered < order.quantity
                ),
            }
        )
    return result


def production_brief(
    snapshot: Snapshot, plan: Candidate | None, order_id: str | None = None
) -> dict:
    """Delivery facts for the Agent to answer from, in factory time with the conclusion first.

    The forecast follows the plan in effect; ``watch`` names what could still change it.
    """
    zone = ZoneInfo(snapshot.profile.timezone)

    def short(value: str | None) -> str | None:
        return (
            datetime.fromisoformat(value).astimezone(zone).strftime("%m-%d %H:%M")
            if value
            else None
        )

    orders = []
    for row in order_facts(snapshot, plan):
        if order_id is not None and row["order_id"] != order_id:
            continue
        if row["status"] == "CANCELLED":
            orders.append({"order_id": row["order_id"], "status": "cancelled"})
            continue
        completion = row["planned_completion_at"]
        early = (
            int(
                (
                    datetime.fromisoformat(row["due_at"]) - datetime.fromisoformat(completion)
                ).total_seconds()
                // 60
            )
            if completion
            else None
        )
        watch = []
        if plan is not None and row["uncovered_quantity"]:
            watch.append(f"{row['uncovered_quantity']} pcs not yet scheduled in the plan in effect")
        if row["direct_shortage_materials"]:
            watch.append("Materials short: " + ", ".join(row["direct_shortage_materials"]))
        if plan is not None and row["forecast_requires_revalidation"] and not watch:
            watch.append(
                "An operation is blocked or behind its planned time, so completion may change"
            )
        orders.append(
            {
                "order_id": row["order_id"],
                "product_id": row["product_id"],
                "quantity": row["quantity"],
                "due": short(row["due_at"]),
                "planned_completion": short(completion),
                "minutes_before_due": early,
                "on_time": early is not None and early >= 0 and not row["uncovered_quantity"],
                "qualified_completed": row["qualified_completed_quantity"],
                "in_progress": row["in_progress_quantity"],
                "planned_ready_today": row["planned_ready_today_quantity"],
                "materials_cover_quantity": row["material_quantity_upper_bound"],
                "watch": watch,
            }
        )
    return {
        "factory_time": snapshot.snapshot_clock.astimezone(zone).strftime("%m-%d %H:%M"),
        "plan_in_effect": plan is not None,
        "orders": orders,
        "material_gaps": [
            f"{g['material_name']} ({g['material_id']}) short by {g['minimum_shortfall']} {g['unit']}"
            for g in material_shortfalls(snapshot)
        ],
    }


def plan_review(snapshot: Snapshot, candidate: Candidate, baseline: Candidate | None) -> dict:
    rows = order_facts(snapshot, candidate)
    previous = {row["order_id"]: row for row in order_facts(snapshot, baseline)}
    for row in rows:
        old = previous[row["order_id"]]
        row["previous_covered_quantity"] = old["plan_covered_quantity"]
        row["previous_completion_at"] = old["planned_completion_at"]
    overtime = []
    workers = {w.worker_id: w for w in snapshot.workers}
    completed = {a.operation_id for a in snapshot.actuals if a.state == "COMPLETED"}
    for assignment in candidate.assignments:
        if assignment.operation_id in completed:
            continue
        start = max(
            snapshot.snapshot_clock,
            assignment.resume_changeover_start or assignment.changeover_start,
        )
        for window in workers[assignment.worker_id].calendar:
            left, right = max(start, window.start_at), min(assignment.end_at, window.end_at)
            if window.kind == "OVERTIME" and right > left:
                overtime.append(
                    {
                        "worker_id": assignment.worker_id,
                        "resource_id": assignment.resource_id,
                        "start_at": local_time(left, snapshot),
                        "end_at": local_time(right, snapshot),
                        "minutes": int((right - left).total_seconds() // 60),
                    }
                )
    return {
        "orders": rows,
        "overtime": overtime,
        "as_of": local_time(snapshot.snapshot_clock, snapshot),
        "accept_before": local_time(candidate.accept_before, snapshot),
    }
