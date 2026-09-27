"""Audited demand revisions preserve production identities and all executed history."""

from packages.domain.execution import OrderChange
from packages.domain.models import (
    FinishedGoodsLot,
    Order,
    Product,
    ProductionBatch,
    Snapshot,
    batch_operations,
)


class DemandChangeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def new_order_batches(
    order: Order, product: Product, *, source_event_id: str
) -> tuple[ProductionBatch, ...]:
    return tuple(
        ProductionBatch(
            batch_id=f"{order.order_id}-R{order.split_revision:03d}-B{sequence:03d}",
            order_id=order.order_id,
            product_id=order.product_id,
            route_version=product.route_version,
            quantity=product.batch_size,
            sequence=sequence,
            purpose="CUSTOMER",
            source_event_id=source_event_id,
            changed_order_version=order.version,
        )
        for sequence in range(1, order.quantity // product.batch_size + 1)
    )


def materialize_batches(snapshot: Snapshot) -> tuple[ProductionBatch, ...]:
    if snapshot.production_batches is not None:
        return snapshot.production_batches
    products = {product.product_id: product for product in snapshot.profile.products}
    return tuple(
        batch
        for order in snapshot.orders
        for batch in new_order_batches(
            order,
            products[order.product_id],
            source_event_id=f"materialize:{snapshot.run_id}:{order.order_id}",
        )
    )


def demand_changes(snapshot: Snapshot, command: OrderChange, *, event_id: str) -> dict:
    """Return a source transition payload; the caller advances revision and records the command."""
    order = next((order for order in snapshot.orders if order.order_id == command.order_id), None)
    if order is None:
        raise DemandChangeError("ORDER_NOT_FOUND")
    if order.version != command.expected_version:
        raise DemandChangeError("ORDER_VERSION_CHANGED")
    product = next(p for p in snapshot.profile.products if p.product_id == order.product_id)
    if command.quantity % product.batch_size:
        raise DemandChangeError("UNSUPPORTED_BATCH_QUANTITY")
    batches = list(materialize_batches(snapshot))
    committed = {actual.batch_id for actual in snapshot.actuals}
    completed = {actual.operation_id for actual in snapshot.actuals if actual.state == "COMPLETED"}
    qualified = {
        actual.operation_id
        for actual in snapshot.actuals
        if actual.state == "COMPLETED" and actual.quality_state == "PASSED"
    }
    route = [step for step in snapshot.profile.routes if step.product_id == order.product_id]
    current = [batch for batch in batches if batch.order_id == order.order_id]
    live = sorted(
        (batch for batch in current if batch.purpose not in {"CANCELLED", "SCRAP"}),
        key=lambda batch: (batch.batch_id not in committed, batch.sequence),
    )
    remaining = command.quantity
    revised = {}
    next_version = order.version + 1
    for batch in live:
        batch_complete = all(
            f"{batch.batch_id}-{step.operation_code}" in completed for step in route
        )
        if (
            batch.purpose == "STOCK"
            and batch_complete
            and any(f"{batch.batch_id}-{step.operation_code}" not in qualified for step in route)
        ):
            # Unqualified completed surplus stays in its original history. Reopened
            # demand needs new production, not a relabelled failed/unknown product.
            continue
        if remaining:
            purpose = "CUSTOMER"
            remaining -= batch.quantity
        else:
            purpose = "STOCK" if batch.batch_id in committed else "CANCELLED"
        due_at = batch.delivery_due_at if purpose == "CUSTOMER" else None
        if command.due_at is not None and due_at == order.due_at and not batch_complete:
            # A change to the final promise moves unfinished final-delivery lots.
            # Earlier partial promises and completed history keep their own dates.
            due_at = command.due_at
        if purpose != batch.purpose or due_at != batch.delivery_due_at:
            revised[batch.batch_id] = batch.model_copy(
                update={
                    "purpose": purpose,
                    "source_event_id": event_id,
                    "changed_order_version": next_version,
                    # A withdrawn customer commitment does not constrain surplus inventory.
                    "delivery_due_at": due_at,
                }
            )
    batches = [revised.get(batch.batch_id, batch) for batch in batches]
    sequence = max((batch.sequence for batch in current), default=0)
    while remaining:
        sequence += 1
        batches.append(
            ProductionBatch(
                batch_id=f"{order.order_id}-R{order.split_revision:03d}-B{sequence:03d}",
                order_id=order.order_id,
                product_id=order.product_id,
                route_version=product.route_version,
                quantity=product.batch_size,
                sequence=sequence,
                purpose="CUSTOMER",
                source_event_id=event_id,
                changed_order_version=next_version,
            )
        )
        remaining -= product.batch_size
    customer = [
        batch
        for batch in batches
        if batch.order_id == order.order_id and batch.purpose == "CUSTOMER"
    ]
    customer_operations = {
        f"{batch.batch_id}-{step.operation_code}"
        for batch in customer
        for step in snapshot.profile.routes
        if step.product_id == batch.product_id
    }
    status = (
        "CANCELLED"
        if command.quantity == 0
        else "COMPLETED"
        if customer_operations <= completed
        else "IN_PROGRESS"
        if any(batch.batch_id in committed for batch in customer)
        else "CONFIRMED"
    )
    raw_order = order.model_dump(mode="python")
    raw_order.update(quantity=command.quantity, version=next_version, status=status)
    if command.due_at is not None:
        raw_order["due_at"] = command.due_at
    changed = Order.model_validate(raw_order)
    return {
        "schema_version": "byof.snapshot/3",
        "production_batches": tuple(batches),
        "orders": tuple(
            changed if item.order_id == order.order_id else item for item in snapshot.orders
        ),
        "scope_version": snapshot.scope_version + 1,
    }


def finished_goods(snapshot: Snapshot) -> tuple[FinishedGoodsLot, ...]:
    """Qualified surplus only: WIP is never a free raw-material or finished-goods balance."""
    actuals = {actual.operation_id: actual for actual in snapshot.actuals}
    _, operations = batch_operations(snapshot)
    result = []
    for batch in snapshot.production_batches or ():
        if batch.purpose != "STOCK":
            continue
        records = [
            actuals.get(operation.operation_id)
            for operation in operations
            if operation.batch_id == batch.batch_id
        ]
        if records and all(
            record is not None and record.state == "COMPLETED" and record.quality_state == "PASSED"
            for record in records
        ):
            result.append(
                FinishedGoodsLot(
                    batch_id=batch.batch_id,
                    product_id=batch.product_id,
                    quantity=batch.quantity,
                    completed_at=max(
                        record.actual_end
                        for record in records
                        if record is not None and record.actual_end is not None
                    ),
                )
            )
    return tuple(result)
