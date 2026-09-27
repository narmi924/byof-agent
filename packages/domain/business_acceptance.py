"""Apply a human-selected business agreement to the simulator's current facts."""

from pydantic import Field, StrictInt

from packages.domain.demand import materialize_batches, new_order_batches
from packages.domain.models import Contract, Digest, Identifier, Order, Snapshot, Timestamp


class BusinessAcceptanceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class BusinessAcceptance(Contract):
    expected_snapshot_hash: Digest
    terms_version: Identifier
    study_hash: Digest
    order: Order | None = None
    first_delivery_quantity: StrictInt = Field(default=0, ge=0)
    first_delivery_due_at: Timestamp | None = None
    final_delivery_due_at: Timestamp | None = None
    quote_id: Identifier | None = None
    confirmed_cost_minor: StrictInt | None = Field(default=None, ge=0)
    currency: str | None = None


def accept_business_option(snapshot: Snapshot, payload: dict, *, event_id: str) -> dict:
    command = BusinessAcceptance.model_validate(payload)
    if command.expected_snapshot_hash != snapshot.content_hash:
        raise BusinessAcceptanceError("BUSINESS_FACTS_CHANGED")
    terms = snapshot.business_terms
    if terms is None or terms.version != command.terms_version:
        raise BusinessAcceptanceError("BUSINESS_TERMS_CHANGED")
    batches = list(materialize_batches(snapshot))
    orders = [
        Order.model_validate(
            {
                **order.model_dump(),
                "hard_deadline": True,
                "version": order.version + int(not order.hard_deadline),
            }
        )
        if order.quantity > 0 and order.status not in {"COMPLETED", "CANCELLED"}
        else order
        for order in snapshot.orders
    ]
    receipts = list(snapshot.receipts)
    if command.order is not None:
        order = command.order
        if any(item.order_id == order.order_id for item in snapshot.orders):
            raise BusinessAcceptanceError("ORDER_ALREADY_EXISTS_OR_STARTED")
        product = next(
            (p for p in snapshot.profile.products if p.product_id == order.product_id), None
        )
        if product is None or order.quantity % product.batch_size:
            raise BusinessAcceptanceError("UNSUPPORTED_BATCH_QUANTITY")
        first = command.first_delivery_quantity
        final = command.final_delivery_due_at
        if (
            order.quantity <= 0
            or order.version != 1
            or order.status != "CONFIRMED"
            or order.split_revision != 1
            or final is None
            or final <= snapshot.snapshot_clock
            or first <= 0
            or first > order.quantity
            or first % product.batch_size
            or command.first_delivery_due_at is None
            or command.first_delivery_due_at <= snapshot.snapshot_clock
            or command.first_delivery_due_at > final
        ):
            raise BusinessAcceptanceError("INVALID_DELIVERY_AGREEMENT")
        if first < order.quantity:
            rule = next((r for r in terms.delivery_rules if r.product_id == order.product_id), None)
            if (
                rule is None
                or not rule.partial_delivery_allowed
                or rule.max_deliveries < 2
                or first < rule.minimum_partial_quantity
            ):
                raise BusinessAcceptanceError("PARTIAL_DELIVERY_NOT_ALLOWED")
        accepted = Order.model_validate(
            {
                **order.model_dump(),
                "requested_due_at": order.due_at,
                "due_at": final,
                "hard_deadline": True,
            }
        )
        new_batches = new_order_batches(accepted, product, source_event_id=event_id)
        remaining = first
        for batch in new_batches:
            due = command.first_delivery_due_at if remaining > 0 else final
            batches.append(batch.model_copy(update={"delivery_due_at": due}))
            remaining -= batch.quantity
        orders.append(accepted)
    elif (
        command.first_delivery_quantity
        or command.final_delivery_due_at
        or command.first_delivery_due_at
    ):
        raise BusinessAcceptanceError("INVALID_DELIVERY_AGREEMENT")
    if command.quote_id is not None:
        quote = next((q for q in terms.expedite_quotes if q.quote_id == command.quote_id), None)
        receipt = next((r for r in receipts if quote and r.receipt_id == quote.receipt_id), None)
        if quote is None or receipt is None:
            raise BusinessAcceptanceError("QUOTE_NOT_FOUND")
        if quote.cost_minor is None:
            raise BusinessAcceptanceError("QUOTE_PRICE_REQUIRED")
        if (
            receipt.status not in {"EXPECTED", "CONFIRMED"}
            or receipt.version != quote.receipt_version
            or receipt.eta != quote.original_eta
            or receipt.quantity != quote.quantity
            or quote.valid_until <= snapshot.snapshot_clock
            or quote.expedited_eta < snapshot.snapshot_clock
            or quote.expedited_eta >= snapshot.horizon.end_at
            or quote.cost_minor != command.confirmed_cost_minor
            or quote.currency != command.currency
        ):
            raise BusinessAcceptanceError("QUOTE_CHANGED_OR_EXPIRED")
        receipts = [
            r.model_copy(
                update={"eta": quote.expedited_eta, "version": r.version + 1, "status": "CONFIRMED"}
            )
            if r.receipt_id == quote.receipt_id
            else r
            for r in receipts
        ]
    elif command.confirmed_cost_minor is not None or command.currency is not None:
        raise BusinessAcceptanceError("INVALID_QUOTE_CONFIRMATION")
    return {
        "schema_version": "byof.snapshot/3",
        "orders": tuple(orders),
        "production_batches": tuple(batches),
        "receipts": tuple(receipts),
        "scope_version": snapshot.scope_version + 1,
    }
