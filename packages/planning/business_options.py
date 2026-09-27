"""Bounded, side-effect-free comparison of promises against one immutable source snapshot.

Derived snapshots and candidates stay inside the study. Applying a business choice requires
fresh source facts and a new formal solve; this module never approves or publishes a plan.
"""

from __future__ import annotations

import multiprocessing
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from packages.domain.business_options import (
    BusinessOption,
    BusinessStudy,
    BusinessStudyRequest,
    DeliveryPromise,
    OrderImpact,
    StudySearch,
)
from packages.domain.business_terms import ExpediteQuote
from packages.domain.demand import materialize_batches, new_order_batches
from packages.domain.models import Candidate, Order, Snapshot, batch_operations, minute_offset
from packages.domain.production_facts import material_shortfalls
from packages.domain.treatment import TreatmentAction, action_cost, material_rate, project_treatment
from packages.planning.business_economics import rank_treatments, with_economics
from packages.planning.checker import check_candidate
from packages.planning.solver import PlanningInputError, solve

MINUTE = timedelta(minutes=1)
PROTECTION = "Unfinished and uncancelled existing orders keep their promised due dates as hard constraints; completed orders keep their actual late records; WIP, freezes, material reservations and full routes are unchanged."
SIMULATION = "A trial of an isolated scenario only: no order accepted, no receipt facts changed, no production plan approved or released."


def _order(order: Order, **changes: Any) -> Order:
    return Order.model_validate({**order.model_dump(), **changes})


def _derive(
    original: Snapshot,
    study_id: str,
    *,
    additions: tuple[Order, ...] = (),
    replacement: Order | None = None,
    quote: ExpediteQuote | None = None,
    protect: bool = True,
    early_delivery: tuple[str, int, datetime] | None = None,
) -> Snapshot:
    data = original.model_dump(mode="python", exclude={"content_hash"})
    data["snapshot_id"] = f"study:{study_id}:{uuid4()}"
    data["orders"] = [
        replacement
        if replacement is not None and order.order_id == replacement.order_id
        else _order(order, hard_deadline=True)
        if protect and order.status not in {"COMPLETED", "CANCELLED"} and order.quantity > 0
        else order
        for order in original.orders
    ] + list(additions)
    if additions:
        data["scope_version"] += 1
        if data.get("production_batches") is not None or early_delivery is not None:
            products = {p.product_id: p for p in original.profile.products}
            data["production_batches"] = (
                *materialize_batches(original),
                *(
                    batch
                    for order in additions
                    for batch in new_order_batches(
                        order, products[order.product_id], source_event_id=f"study:{study_id}"
                    )
                ),
            )
    if early_delivery is not None:
        order_id, quantity, due_at = early_delivery
        marked = []
        batches = data["production_batches"] if additions else materialize_batches(original)
        for batch in batches:
            if batch.order_id == order_id and batch.purpose == "CUSTOMER" and quantity > 0:
                batch = batch.model_copy(update={"delivery_due_at": due_at})
                quantity -= batch.quantity
            marked.append(batch)
        if quantity != 0:
            raise ValueError("First delivery must consist of complete production batches")
        data["production_batches"] = tuple(marked)
        data["schema_version"] = "byof.snapshot/3"
    if quote is not None:
        data["receipts"] = [
            {**receipt, "eta": quote.expedited_eta, "status": "CONFIRMED"}
            if receipt["receipt_id"] == quote.receipt_id
            else receipt
            for receipt in data["receipts"]
        ]
    return Snapshot.model_validate(data)


def _batch_finishes(
    snapshot: Snapshot, candidate: Candidate
) -> dict[str, list[tuple[int, datetime]]]:
    """Only all terminal operations finishing makes a complete batch available to promise."""
    batches, operations = batch_operations(snapshot)
    steps = {step.step_id: step for step in snapshot.profile.routes}
    predecessors = {parent for step in steps.values() for parent in step.predecessors}
    assignments = {a.operation_id: a for a in candidate.assignments}
    by_batch: dict[str, list[datetime]] = defaultdict(list)
    for operation in operations:
        if operation.step_id not in predecessors:
            assignment = assignments.get(operation.operation_id)
            if assignment is not None:
                by_batch[operation.batch_id].append(assignment.end_at)
    result: dict[str, list[tuple[int, datetime]]] = defaultdict(list)
    purposes = {batch.batch_id: batch.purpose for batch in snapshot.production_batches or ()}
    for batch in batches:
        if purposes.get(batch.batch_id, "CUSTOMER") != "CUSTOMER":
            continue
        if by_batch[batch.batch_id]:
            result[batch.order_id].append((batch.quantity, max(by_batch[batch.batch_id])))
    return result


def _impacts(
    source: Snapshot,
    derived: Snapshot,
    candidate: Candidate,
    baseline: Candidate | None,
    request: BusinessStudyRequest,
    aliases: dict[str, str],
) -> tuple[OrderImpact, ...]:
    before = _batch_finishes(source, baseline) if baseline is not None else {}
    combined: dict[str, list[tuple[int, datetime]]] = defaultdict(list)
    for identifier, batches in _batch_finishes(derived, candidate).items():
        combined[aliases.get(identifier, identifier)].extend(batches)
    orders = (*source.orders, *((request.order,) if request.order is not None else ()))
    result = []
    for order in orders:
        completed = combined.get(order.order_id, [])
        finish = max((at for _, at in completed), default=None)
        old_finish = max((at for _, at in before.get(order.order_id, [])), default=None)
        result.append(
            OrderImpact(
                order_id=order.order_id,
                existing_commitment=order.order_id in {o.order_id for o in source.orders},
                requested_due_at=order.due_at,
                quantity=next(
                    (o.quantity for o in derived.orders if o.order_id == order.order_id),
                    order.quantity,
                )
                if request.kind == "production_exception"
                else order.quantity,
                on_time_quantity=sum(quantity for quantity, at in completed if at <= order.due_at),
                completion_at=finish,
                tardiness_minutes=max(0, minute_offset(order.due_at, finish, round_up=True))
                if finish is not None
                else (0 if order.quantity == 0 else None),
                baseline_completion_at=old_finish,
                completion_change_minutes=minute_offset(old_finish, finish, round_up=True)
                if old_finish is not None and finish is not None
                else None,
            )
        )
    return tuple(result)


def _checked_attempt(
    derived: Snapshot,
    overtime: bool,
    seconds: float,
    condition: str,
    baseline: Candidate | None,
) -> tuple[Candidate | None, str, StudySearch]:
    """Solve one derived scenario and check it independently; safe to run in a worker process."""
    try:
        candidate = solve(derived, time_limit=seconds, allow_overtime=overtime, baseline=baseline)
    except (PlanningInputError, ValidationError) as exc:
        return (
            None,
            "BLOCKED",
            StudySearch(
                condition=condition,
                time_limit_seconds=seconds,
                native_status="NOT_RUN",
                checked_solution=False,
                error_code=exc.code if isinstance(exc, PlanningInputError) else "INVALID_SCENARIO",
            ),
        )
    report = check_candidate(derived, candidate, baseline=baseline, allow_overtime=overtime)
    checked = candidate.has_solution and report.status == "PASS"
    if candidate.has_solution and report != candidate.checker:
        data = candidate.model_dump(exclude={"content_hash"})
        data["checker"] = report
        candidate = Candidate.model_validate(data)
    search = StudySearch(
        condition=condition,
        time_limit_seconds=seconds,
        native_status=candidate.native_status,
        checked_solution=checked,
        candidate_hash=candidate.content_hash,
    )
    if checked:
        return candidate, "FEASIBLE", search
    if candidate.has_solution:
        return candidate, "CHECK_FAILED", search
    if candidate.native_status == "INFEASIBLE":
        return candidate, "INFEASIBLE", search
    return candidate, "UNKNOWN", search


def option_workers() -> int:
    """Concurrent option solves; each solve stays single-threaded and seeded.

    BYOF_OPTION_WORKERS overrides the default, which leaves one CPU for the services.
    """
    configured = os.environ.get("BYOF_OPTION_WORKERS", "").strip()
    if configured:
        return max(1, int(configured))
    available = (
        len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    )
    return max(1, min(12, available - 1))


class _Study:
    def __init__(
        self, snapshot: Snapshot, baseline: Candidate | None, request: BusinessStudyRequest
    ):
        self.started = time.perf_counter()
        self.deadline = self.started + request.total_time_limit
        self.study_id = str(uuid4())
        self.source, self.baseline, self.request = snapshot, baseline, request
        self.target_order = request.order or next(
            (order for order in snapshot.orders if order.order_id == request.existing_order_id),
            None,
        )

    def derive_order(
        self, order: Order, *, early_delivery: tuple[str, int, datetime] | None = None
    ) -> Snapshot:
        return _derive(
            self.source,
            self.study_id,
            additions=() if self.request.existing_order_id is not None else (order,),
            replacement=order if self.request.existing_order_id is not None else None,
            early_delivery=early_delivery,
        )

    def remaining(self, deadline: float) -> float:
        return max(0.0, min(self.deadline, deadline) - time.perf_counter())

    def attempt(
        self,
        derived: Snapshot,
        overtime: bool,
        deadline: float,
        condition: str,
        searches: list[StudySearch],
        *,
        searching: bool = False,
    ) -> tuple[Candidate | None, str]:
        remaining = self.remaining(deadline)
        if remaining < 0.02:
            return None, "BUDGET_EXHAUSTED"
        seconds = min(remaining, max(0.02, remaining / 3)) if searching else remaining
        candidate, status, search = _checked_attempt(
            derived, overtime, seconds, condition, self.baseline
        )
        searches.append(search)
        return candidate, status

    def option(
        self,
        *,
        kind: str,
        title: str,
        status: str,
        summary: str,
        assumptions: tuple[str, ...] = (),
        derived: Snapshot | None = None,
        candidate: Candidate | None = None,
        overtime: bool = False,
        quote: ExpediteQuote | None = None,
        aliases: dict[str, str] | None = None,
        searches: list[StudySearch] | None = None,
        earliest_proven: bool = False,
        quantity_proven: bool = False,
        diagnostic: bool = False,
        agreed: dict[str, datetime] | None = None,
    ) -> BusinessOption:
        impacts = (
            _impacts(self.source, derived, candidate, self.baseline, self.request, aliases or {})
            if status == "FEASIBLE" and derived is not None and candidate is not None
            else ()
        )
        outstanding_orders = {
            order.order_id
            for order in self.source.orders
            if order.quantity > 0
            and order.status not in {"COMPLETED", "CANCELLED"}
            and order.order_id != self.request.existing_order_id
        }
        protected = (
            all(
                impact.tardiness_minutes == 0
                for impact in impacts
                if impact.existing_commitment and impact.order_id in outstanding_orders
            )
            if impacts
            else None
        )
        target = next(
            (i for i in impacts if self.target_order and i.order_id == self.target_order.order_id),
            None,
        )
        deliveries = []
        if (
            kind == "earliest_completion"
            and target is not None
            and target.completion_at is not None
        ):
            # This option negotiates one complete shipment. Earlier batch availability
            # remains an impact statistic, not an additional delivery commitment.
            deliveries.append(
                DeliveryPromise(quantity=target.quantity, ready_at=target.completion_at)
            )
        elif target is not None and target.completion_at is not None:
            # Display the same delivery promises that business confirmation will save.
            # Earlier physical readiness remains visible in completion/impact metrics.
            first = target.on_time_quantity if kind == "partial_delivery" else target.quantity
            assert first is not None and first > 0
            promised = (agreed or {}).get(target.order_id, target.requested_due_at)
            deliveries.append(DeliveryPromise(quantity=first, ready_at=promised))
            if first < target.quantity:
                deliveries.append(
                    DeliveryPromise(
                        quantity=target.quantity - first,
                        ready_at=max(target.requested_due_at, target.completion_at),
                    )
                )
        metrics = (
            {m.name: m.value for m in candidate.objective}
            if status == "FEASIBLE" and candidate
            else {}
        )
        return BusinessOption.model_validate(
            {
                "option_id": str(uuid4()),
                "kind": kind,
                "title": title,
                "status": status,
                "summary": summary,
                "assumptions": (
                    SIMULATION,
                    *(
                        ()
                        if diagnostic
                        else (
                            PROTECTION.replace(
                                "existing orders", "existing orders other than the selected order"
                            )
                            if self.request.existing_order_id is not None
                            else PROTECTION,
                        )
                    ),
                    *(
                        (
                            "Only compares the delivery proposal of the selected source order; the original commitments of other orders are protected and the source order and its batches were not rewritten.",
                        )
                        if self.request.existing_order_id is not None
                        else ()
                    ),
                    *assumptions,
                ),
                "allow_overtime": overtime,
                "diagnostic_only": diagnostic,
                "protects_existing_commitments": protected,
                "quote_id": quote.quote_id if quote else None,
                "cost_minor": quote.cost_minor if quote else None,
                "currency": quote.currency if quote else None,
                "incremental_overtime_minutes": metrics.get("incremental_overtime_metric"),
                "changed_operations": metrics.get("changed_operations"),
                "total_start_shift_minutes": metrics.get("total_start_shift"),
                "requested_quantity": self.target_order.quantity if self.target_order else None,
                "on_time_quantity": target.on_time_quantity if target else None,
                "completion_at": target.completion_at
                if target
                else max((i.completion_at for i in impacts if i.completion_at), default=None),
                "deliveries": deliveries,
                "earliest_completion_proven": earliest_proven,
                "maximum_on_time_quantity_proven": quantity_proven,
                "impacts": impacts,
                "searches": searches or [],
                "derived_snapshot": derived,
                "candidate": candidate,
            }
        )

    def fixed(
        self,
        kind: str,
        title: str,
        deadline: float,
        *,
        overtime: bool = False,
        quote: ExpediteQuote | None = None,
        diagnostic: bool = False,
    ) -> BusinessOption:
        derived = (
            self.derive_order(_order(self.target_order, hard_deadline=True))
            if self.target_order is not None
            else _derive(self.source, self.study_id, quote=quote, protect=not diagnostic)
        )
        searches: list[StudySearch] = []
        candidate, status = self.attempt(
            derived, overtime, deadline, "Keep a full schedule for all demand", searches
        )
        notes: tuple[str, ...] = (
            ("Only shows what waiting would lead to; it is not used to promise a new delivery.",)
            if diagnostic
            else ()
        )
        if quote:
            notes += (
                f"Uses source quote {quote.quote_id}; the ETA of the whole receipt moves earlier and the quantity does not increase.",
                "The quote comes from the supplier quote catalog."
                if quote.evidence_mode == "synthetic"
                else "The quote comes from the enterprise source.",
            )
        return self.option(
            kind=kind,
            title=title,
            status=status,
            summary="A checked trial schedule was found."
            if status == "FEASIBLE"
            else _failure(status),
            assumptions=notes,
            derived=derived,
            candidate=candidate,
            overtime=overtime,
            quote=quote,
            searches=searches,
            diagnostic=diagnostic,
        )

    def earliest(self, deadline: float) -> BusinessOption:
        order = self.target_order
        assert order is not None
        final = self.request.final_due_at or self.source.horizon.end_at
        origin = self.source.horizon.start_at
        lower = minute_offset(origin, self.source.snapshot_clock, round_up=False) - 1
        upper = minute_offset(origin, min(final, self.source.horizon.end_at), round_up=False)
        searches: list[StudySearch] = []
        best: tuple[Snapshot, Candidate] | None = None
        last_derived = None
        last_candidate = None
        status = "BUDGET_EXHAUSTED"
        proven = False
        # First establish a feasible upper bound, then only INFEASIBLE raises the lower bound.
        for _ in range(24):
            bound = upper if best is None else (lower + upper) // 2
            last_derived = self.derive_order(
                _order(order, hard_deadline=True, due_at=origin + bound * MINUTE),
            )
            last_candidate, status = self.attempt(
                last_derived,
                True,
                deadline,
                f"The selected order completes fully no later than {bound} minutes",
                searches,
                searching=True,
            )
            if status == "FEASIBLE":
                assert last_candidate is not None
                best = last_derived, last_candidate
                actual = max(
                    at for _, at in _batch_finishes(last_derived, last_candidate)[order.order_id]
                )
                upper = minute_offset(origin, actual, round_up=True)
            elif status == "INFEASIBLE" and best is not None:
                lower = bound
            else:
                break
            if best is not None and upper - lower <= 1:
                proven = True
                break
        if best is not None:
            last_derived, last_candidate = best
            status = "FEASIBLE"
        return self.option(
            kind="earliest_completion",
            title="Negotiate a full delivery date",
            status=status,
            summary=(
                "This is the earliest full delivery."
                if proven
                else "This full delivery time works; an earlier one may still be possible."
            )
            if best
            else _failure(status),
            assumptions=(
                "Overtime allowed; the selected order date is a due date proposal awaiting customer confirmation, and the customer's original request stays in the study record.",
            ),
            derived=last_derived,
            candidate=last_candidate,
            overtime=True,
            searches=searches,
            earliest_proven=proven,
        )

    def partial(self, deadline: float, normal: BusinessOption) -> BusinessOption:
        order = self.target_order
        assert order is not None
        product = next(p for p in self.source.profile.products if p.product_id == order.product_id)
        count = order.quantity // product.batch_size
        minimum = self.request.minimum_partial_quantity or product.batch_size
        low = (minimum + product.batch_size - 1) // product.batch_size
        high = count - 1
        searches: list[StudySearch] = []
        aliases: dict[str, str] = {}
        best: tuple[Snapshot, Candidate] | None = None
        last_derived, last_candidate = None, None
        status = "BUDGET_EXHAUSTED"
        full_impossible = normal.status == "INFEASIBLE"
        if normal.status == "FEASIBLE":
            return self.option(
                kind="partial_delivery",
                title="Split delivery as permitted",
                status="FEASIBLE",
                summary="The whole order can already finish on time; no split delivery is needed for the due date.",
                derived=normal.derived_snapshot,
                candidate=normal.candidate,
                quantity_proven=True,
                assumptions=("Regular shifts; the customer allows at most two deliveries.",),
            )
        if low > high:
            return self.option(
                kind="partial_delivery",
                title="Split delivery as permitted",
                status="BLOCKED",
                summary="The minimum first delivery or whole-batch constraints do not allow two non-empty deliveries.",
            )
        final = self.request.final_due_at or self.source.horizon.end_at
        # Existence is monotone in the first delivery quantity; every test keeps all production.
        for _ in range(24):
            if low > high:
                break
            trial = (low + high) // 2
            early_quantity = trial * product.batch_size
            whole = _order(order, due_at=final, hard_deadline=True)
            last_derived = self.derive_order(
                whole,
                early_delivery=(order.order_id, early_quantity, order.due_at),
            )
            last_candidate, status = self.attempt(
                last_derived,
                False,
                deadline,
                f"First delivery at least {early_quantity} pcs and the rest fully scheduled",
                searches,
                searching=True,
            )
            if status == "FEASIBLE":
                assert last_candidate is not None
                best = last_derived, last_candidate
                finishes = _batch_finishes(last_derived, last_candidate)
                on_time = sum(q for q, at in finishes[order.order_id] if at <= order.due_at)
                low = max(trial + 1, on_time // product.batch_size + 1)
            elif status == "INFEASIBLE":
                high = trial - 1
            else:
                break
        if best:
            last_derived, last_candidate = best
            status = "FEASIBLE"
        elif status == "INFEASIBLE" and low <= high:
            status = "UNKNOWN"
        proven = bool(best and low > high and full_impossible)
        return self.option(
            kind="partial_delivery",
            title="Split delivery as permitted",
            status=status,
            summary=(
                "This is the largest quantity that can be delivered on time."
                if proven
                else "This split delivery works; more may still be possible on time."
            )
            if best
            else _failure(status),
            assumptions=(
                "Regular shifts; at most two deliveries; both parts fully produced and the final delivery date constrained.",
            ),
            derived=last_derived,
            candidate=last_candidate,
            aliases=aliases,
            searches=searches,
            quantity_proven=proven,
        )


def _failure(status: str) -> str:
    return {
        "INFEASIBLE": "No schedule can keep the existing due dates.",
        "UNKNOWN": "No feasible schedule found within the time limit.",
        "BLOCKED": "The shop floor is still missing required facts (such as the remaining work of a blocked operation), so this cannot be calculated yet.",
        "BUDGET_EXHAUSTED": "The calculation time ran out before this option finished.",
        "CHECK_FAILED": "The calculated schedule failed the independent check and cannot be used.",
    }.get(status, "This option is not finished.")


def _quote_error(
    snapshot: Snapshot, request: BusinessStudyRequest, quote: ExpediteQuote | None
) -> str | None:
    if quote is None:
        return "The selected quote is not among the quotes provided by this source."
    receipt = next((r for r in snapshot.receipts if r.receipt_id == quote.receipt_id), None)
    if request.receipt_id is not None and request.receipt_id != quote.receipt_id:
        return "The quote does not belong to the selected receipt."
    if (
        receipt is None
        or receipt.status not in {"CONFIRMED", "EXPECTED"}
        or receipt.version != quote.receipt_version
        or receipt.eta != quote.original_eta
        or receipt.quantity != quote.quantity
    ):
        return "The receipt version, arrival time, quantity or confirmation no longer matches the quote."
    if quote.valid_until <= snapshot.snapshot_clock:
        return "The quote has expired by the factory business clock."
    if (
        quote.expedited_eta < snapshot.snapshot_clock
        or quote.expedited_eta >= snapshot.horizon.end_at
    ):
        return "The quoted arrival must be after the current time and inside the scheduling window."
    return None


def _existing_order_block(snapshot: Snapshot, order: Order) -> str | None:
    if order.status != "CONFIRMED" or order.quantity <= 0:
        return "The selected source order is not waiting to start; this delivery comparison does not support started, completed or cancelled orders."
    batches = [batch for batch in materialize_batches(snapshot) if batch.order_id == order.order_id]
    identities = {batch.batch_id for batch in batches}
    if any(actual.batch_id in identities for actual in snapshot.actuals):
        return "The selected source order already has execution records; its batches and actuals are kept, and this delivery comparison does not recreate or split WIP."
    if any(batch.delivery_due_at is not None for batch in batches if batch.purpose == "CUSTOMER"):
        return "The selected order already has a split-delivery commitment; this comparison does not overwrite existing batch due dates. Check the customer agreement first."
    if sum(batch.quantity for batch in batches if batch.purpose == "CUSTOMER") != order.quantity:
        return "The customer batch quantities of the selected order do not match current demand; check the source facts first."
    return None


def timing_gaps(source: Snapshot, baseline: Candidate | None) -> dict[str, int]:
    """Material the plan in effect needs at its kit starts before confirmed deliveries arrive.

    Uses the same arithmetic as the independent Checker; a later delivery shows up here even
    when the total quantity within the planning window is still enough.
    """
    if baseline is None:
        return {}
    from packages.planning.checker import _required_operations

    starts = {a.operation_id: a.start_at for a in baseline.assignments}
    started = {a.batch_id for a in source.actuals if a.actual_start is not None}
    bom: dict[str, list] = {}
    for item in source.profile.bom:
        bom.setdefault(item.product_id, []).append(item)
    demand: dict[str, list[tuple[datetime, int]]] = {}
    for operation_id, operation in _required_operations(source).items():
        if (
            operation.step.predecessors
            or operation.batch_id in started
            or operation_id not in starts
        ):
            continue
        for item in bom.get(operation.product_id, ()):
            demand.setdefault(item.material_id, []).append(
                (starts[operation_id], item.quantity_per_unit * operation.quantity)
            )
    stocks = {s.material_id: s for s in source.inventory}
    gaps = {}
    for material_id, requests in demand.items():
        if material_id not in stocks:
            continue
        balance = stocks[material_id].on_hand - stocks[material_id].reserved
        incoming = sorted(
            (r.eta, r.quantity)
            for r in source.receipts
            if r.material_id == material_id and r.status == "CONFIRMED"
        )
        index, worst = 0, 0
        for at, quantity in sorted(requests):
            while index < len(incoming) and incoming[index][0] <= at:
                balance += incoming[index][1]
                index += 1
            balance -= quantity
            worst = max(worst, -balance)
        if worst:
            gaps[material_id] = worst
    return gaps


def _negotiable(
    projected: Snapshot, study_id: str, target: Order | None, *, probing: bool
) -> list[Snapshot]:
    """Scenarios in which promise dates may move with customer agreement.

    With an order in question every other open promise stays a hard constraint, and that order
    is also tried against later dates: a deadline guides the search to an early completion
    where a free date alone leaves it at the end of the queue. A promise the customer marked as
    firm may move too: the customer's agreement is confirmed on the card before execution.
    """
    if target is None:
        orders = tuple(
            _order(o, hard_deadline=False) if o.status in {"CONFIRMED", "IN_PROGRESS"} else o
            for o in projected.orders
        )
        return [_derive(projected.model_copy(update={"orders": orders}), study_id, protect=False)]
    order = next(o for o in projected.orders if o.order_id == target.order_id)
    free = _derive(projected, study_id, replacement=_order(order, hard_deadline=False))
    if not probing:
        return [free]
    dates = sorted(
        {
            min(order.due_at + delta, projected.horizon.end_at - MINUTE)
            for delta in (timedelta(days=1), timedelta(days=2))
        }
    )
    return [
        *(
            _derive(projected, study_id, replacement=_order(order, due_at=date, hard_deadline=True))
            for date in dates
            if date > order.due_at
        ),
        free,
    ]


def _agreed_date(source: Snapshot, completion: datetime) -> datetime | None:
    """A whole local hour at least half an hour after the checked completion."""
    local = (completion + timedelta(minutes=30)).astimezone(ZoneInfo(source.profile.timezone))
    hour = local.replace(minute=0, second=0, microsecond=0)
    if hour < local:
        hour += timedelta(hours=1)
    date = min(hour.astimezone(UTC), source.horizon.end_at - MINUTE)
    return date if date >= completion else None


def _date_changes(
    source: Snapshot, derived: Snapshot, candidate: Candidate, study_id: str, index: int
) -> tuple[TreatmentAction, ...] | None:
    """A proposed date for each open order the checked plan completes after its promise."""
    finishes = _batch_finishes(derived, candidate)
    promised = {order.order_id: order.due_at for order in source.orders}
    changes = []
    for order in derived.orders:
        if order.status not in {"CONFIRMED", "IN_PROGRESS"} or order.quantity <= 0:
            continue
        finish = max((at for _, at in finishes.get(order.order_id, ())), default=None)
        if finish is None or finish <= promised.get(order.order_id, order.due_at):
            continue
        date = _agreed_date(source, finish)
        if date is None:
            return None
        changes.append(
            TreatmentAction(
                kind="order_due",
                target_id=order.order_id,
                action_id=f"due:{study_id}:{index}:{order.order_id}",
                expected_version=order.version,
                ready_at=date,
            )
        )
    return tuple(changes)


def _treatment_options(study: _Study) -> list[BusinessOption]:
    source, request = study.source, study.request
    now = source.snapshot_clock
    base: list[TreatmentAction] = []
    # An interrupted operation stands for the disruption behind it: every current one is treated.
    subject = (
        None
        if request.subject_id in {a.operation_id for a in source.actuals}
        else request.subject_id
    )
    for resource in source.resources:
        if subject and subject != resource.resource_id:
            continue
        if resource.status in {"DOWN", "MAINTENANCE"} or any(
            w.start_at <= now < w.end_at for w in resource.unavailable
        ):
            base.append(
                TreatmentAction(
                    kind="repair",
                    target_id=resource.resource_id,
                    action_id=f"repair:{study.study_id}:{resource.resource_id}",
                    expected_version=resource.version,
                    ready_at=now + timedelta(minutes=75),
                )
            )
    for worker in source.workers:
        if worker.status == "ABSENT" and (subject is None or subject == worker.worker_id):
            base.append(
                TreatmentAction(
                    kind="staff",
                    target_id=worker.worker_id,
                    action_id=f"agency:{study.study_id}:{worker.worker_id}",
                    expected_version=worker.version,
                    ready_at=now + timedelta(minutes=45),
                )
            )
    shortfalls = material_shortfalls(source)
    quantity_short = bool(shortfalls)
    # Deliveries that now arrive after the plan in effect needs them are timing gaps: offer buying
    # that quantity again. Without a plan in effect, a named late receipt or material stands in.
    receipt = next((r for r in source.receipts if r.receipt_id == subject), None)
    material = receipt.material_id if receipt else subject
    timing = timing_gaps(source, study.baseline)
    if not timing and study.baseline is None:
        late = sum(
            r.quantity
            for r in source.receipts
            if r.material_id == material
            and r.status in {"CONFIRMED", "EXPECTED"}
            and r.eta > now + timedelta(minutes=135)
        )
        if late and any(s.material_id == material for s in source.inventory):
            timing = {str(material): late}
    known = {gap["material_id"] for gap in shortfalls}
    shortfalls = [
        *shortfalls,
        *(
            {"material_id": material_id, "minimum_shortfall": deficit}
            for material_id, deficit in sorted(timing.items())
            if material_id not in known
        ),
    ]
    # Each bundle: title, measures, overtime, and whether promise dates may be negotiated.
    bundles: list[tuple[str, tuple[TreatmentAction, ...], bool, bool]] = [
        ("Schedule under current conditions", (), False, False),
        ("Use existing qualified overtime windows", (), True, False),
    ]
    modes = ("standard", "express", "immediate") if shortfalls else ("standard",)
    for mode in modes:
        actions = list(base)
        supported = True
        for gap in shortfalls:
            quantity = ((gap["minimum_shortfall"] + 49) // 50) * 50
            if quantity > 5000 or material_rate(gap["material_id"]) is None:
                supported = False
                break
            stock = next(s for s in source.inventory if s.material_id == gap["material_id"])
            minutes = {"standard": 135, "express": 45, "immediate": 0}[mode]
            actions.append(
                TreatmentAction.model_validate(
                    {
                        "kind": "supply",
                        "target_id": stock.material_id,
                        "quantity": quantity,
                        "action_id": f"supply:{study.study_id}:{mode}:{stock.material_id}",
                        "expected_version": stock.version,
                        "ready_at": now + timedelta(minutes=minutes),
                        "mode": mode,
                    }
                )
            )
        if supported and actions:
            label = (
                {
                    "standard": "Standard resupply and required resource measures",
                    "express": "Expedited resupply and required resource measures",
                    "immediate": "Immediate transfer from local emergency stock",
                }[mode]
                if shortfalls
                else "Expedited repair or qualified cover"
            )
            bundles.append((label, tuple(actions), False, False))
    protected = len(bundles)
    target = next(
        (o for o in source.orders if o.order_id == (request.existing_order_id or subject)), None
    )
    if target is None and shortfalls:
        # The earliest unfinished order that uses a short or late material may negotiate its date.
        missing = {gap["material_id"] for gap in shortfalls}
        users = {b.product_id for b in source.profile.bom if b.material_id in missing}
        target = min(
            (
                o
                for o in source.orders
                if o.product_id in users and o.status in {"CONFIRMED", "IN_PROGRESS"}
            ),
            key=lambda o: o.due_at,
            default=None,
        )
    # When no measure keeps every promise, a checked plan still exists once dates move: the
    # ordinary measures, and the fastest ones with overtime, each propose the dates they reach.
    ordinary = bundles[2][1] if protected > 2 else ()
    fastest = bundles[protected - 1][1] if protected > 2 else ()
    ordinary_title = bundles[2][0] if protected > 2 else "Schedule under current conditions"
    fastest_title = (
        f"{bundles[protected - 1][0]}, with overtime"
        if protected > 2
        else "Use existing qualified overtime windows"
    )
    if any(o.status in {"CONFIRMED", "IN_PROGRESS"} and o.quantity > 0 for o in source.orders):
        bundles.append(
            (
                "Negotiate later due dates with standard measures"
                if ordinary
                else "Negotiate later due dates under current conditions",
                ordinary,
                False,
                True,
            )
        )
        calendars = [*(w.calendar for w in source.workers), *(r.calendar for r in source.resources)]
        if fastest != ordinary or any(
            window.kind == "OVERTIME" and window.end_at > now
            for calendar in calendars
            for window in calendar
        ):
            bundles.append(
                (
                    "Negotiate later due dates with expedited measures and overtime"
                    if fastest
                    else "Negotiate later due dates with overtime",
                    fastest,
                    True,
                    True,
                )
            )
    negotiated = len(bundles)
    if target and target.status == "CONFIRMED":
        product = next(p for p in source.profile.products if p.product_id == target.product_id)
        # A demand concession should also fit the ordinary confirmed supply, rather
        # than merely reduce an arbitrary percentage while preserving the shortage.
        gaps = {gap["material_id"]: gap["minimum_shortfall"] for gap in shortfalls}
        reduction = max(
            (
                int(-(-gaps.get(bom.material_id, 0) // bom.quantity_per_unit))
                for bom in source.profile.bom
                if bom.product_id == target.product_id
            ),
            default=0,
        )
        quantity = (
            min(target.quantity * 4 // 5, target.quantity - reduction) // product.batch_size
        ) * product.batch_size
        if 0 < quantity < target.quantity and quantity <= 5000:
            change = TreatmentAction(
                kind="order_quantity",
                target_id=target.order_id,
                action_id=f"quantity:{study.study_id}:{target.order_id}",
                expected_version=target.version,
                ready_at=now + timedelta(minutes=15),
                quantity=quantity,
            )
            reduced = project_treatment(source, (change,), f"reduced:{study.study_id}")
            supplies = []
            for gap in material_shortfalls(reduced):
                amount = ((gap["minimum_shortfall"] + 49) // 50) * 50
                if amount > 5000 or material_rate(gap["material_id"]) is None:
                    break
                stock = next(s for s in source.inventory if s.material_id == gap["material_id"])
                supplies.append(
                    TreatmentAction(
                        kind="supply",
                        target_id=stock.material_id,
                        action_id=f"reduced-supply:{study.study_id}:{stock.material_id}",
                        expected_version=stock.version,
                        ready_at=now + timedelta(minutes=135),
                        quantity=amount,
                    )
                )
            else:
                bundles.append(
                    (
                        "Negotiate a smaller order quantity to cut resupply cost"
                        if shortfalls
                        else "Negotiate a smaller order quantity",
                        (change, *base, *supplies),
                        False,
                        True,
                    )
                )
    movable_target = target if target and target.status in {"CONFIRMED", "IN_PROGRESS"} else None
    # Scenarios keyed by bundle and probe: a negotiated bundle tries several dates at once.
    prepared: dict[tuple[int, int], Snapshot] = {}
    for index, (_, bundle_actions, _, movable) in enumerate(bundles):
        if index < 2 and shortfalls:
            continue
        try:
            if len(bundle_actions) > 24:
                raise ValueError("Treatment bundle exceeds supported capacity")
            projected = project_treatment(
                source, bundle_actions, f"treatment:{study.study_id}:{index}"
            )
        except ValueError:
            continue
        scenarios = (
            _negotiable(projected, study.study_id, movable_target, probing=index < negotiated)
            if movable
            else [_derive(projected, study.study_id)]
        )
        for probe, derived in enumerate(scenarios):
            prepared[index, probe] = derived
    workers = min(option_workers(), len(prepared))
    concurrent: dict[tuple[int, int], tuple[Candidate | None, str, StudySearch]] = {}
    if workers > 1:
        # Each option runs in its own process; spawn keeps database connections and service
        # threads out of the children. A scenario without a schedule after forty seconds stays
        # unknown, so the manager is not kept waiting while the other options already answer.
        seconds = min(study.remaining(study.deadline), 40)
        with ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            futures = {
                key: pool.submit(
                    _checked_attempt,
                    derived,
                    bundles[key[0]][2],
                    seconds,
                    bundles[key[0]][0],
                    study.baseline,
                )
                for key, derived in prepared.items()
            }
            concurrent = {key: future.result() for key, future in futures.items()}

    def finish(derived: Snapshot, candidate: Candidate | None) -> datetime:
        assert candidate is not None
        return max(
            (
                at
                for order_id, batches in _batch_finishes(derived, candidate).items()
                if movable_target is None or order_id == movable_target.order_id
                for _, at in batches
            ),
            default=derived.horizon.end_at,
        )

    options = []
    for index, (title, bundle_actions, overtime, movable) in enumerate(bundles):
        if index < 2 and shortfalls:
            options.append(
                study.option(
                    kind="normal" if index == 0 else "overtime",
                    title=title,
                    status="BLOCKED",
                    summary="The verified material total cannot cover all demand; rescheduling or overtime alone cannot close the quantity gap."
                    if quantity_short
                    else "Material arrives later than production needs it; rescheduling or overtime alone cannot kit it on time.",
                )
            )
            continue
        deadline = time.perf_counter() + study.remaining(study.deadline) / (len(bundles) - index)
        try:
            keys = [key for key in prepared if key[0] == index]
            if not keys:
                raise ValueError("Treatment bundle could not be projected")
            searches: list[StudySearch] = []
            tried = []
            for key in keys:
                if key in concurrent:
                    candidate, status, search = concurrent[key]
                    searches.append(search)
                else:
                    candidate, status = study.attempt(
                        prepared[key], overtime, deadline, title, searches, searching=len(keys) > 1
                    )
                tried.append((prepared[key], candidate, status))
            # The earliest checked completion across probes; without one, the freest scenario.
            feasible = [attempt for attempt in tried if attempt[2] == "FEASIBLE"]
            derived, candidate, status = (
                min(feasible, key=lambda attempt: finish(attempt[0], attempt[1]))
                if feasible
                else tried[-1]
            )
            measures = bundle_actions
            if movable and status == "FEASIBLE":
                assert candidate is not None
                changes = _date_changes(source, derived, candidate, study.study_id, index)
                if changes is None:
                    options.append(
                        study.option(
                            kind="treatment",
                            title=title,
                            status="BLOCKED",
                            summary="With these measures the order is complete only at the end of the scheduling window, so no new due date can be proposed within it.",
                        )
                    )
                    continue
                measures = (*bundle_actions, *changes)
                if len(measures) > 24:
                    raise ValueError("Treatment bundle exceeds supported capacity")
                project_treatment(source, measures, f"negotiated:{study.study_id}:{index}")
                if not changes and index < negotiated:
                    # Every promise holds: the same measures without any date to negotiate.
                    title = fastest_title if overtime else ordinary_title
            option = study.option(
                kind="normal" if index == 0 else "overtime" if index == 1 else "treatment",
                title=title,
                status=status,
                summary="A schedule that passed the independent check was found; the purchases, receipts, repairs or cover below are measures awaiting approval."
                if status == "FEASIBLE"
                else _failure(status),
                derived=derived,
                candidate=candidate,
                overtime=overtime,
                searches=searches,
                assumptions=(
                    "Quotes are valid for 15 minutes from the current business time; resupply comes in packs of 50 units, and each material may be bought at most 5,000 in total per run, which splitting cannot get around.",
                    "Expedited repair takes at least 60 minutes; qualified contract cover at least 30 minutes, and the absence record of the original worker is kept.",
                    "A due date or quantity change only updates the delivery commitment after the manager confirms the customer agreed.",
                    *(
                        (
                            "An order that cannot meet its original due date gets a proposed new date on the first whole hour at least 30 minutes after its scheduled completion.",
                        )
                        if movable
                        else ()
                    ),
                ),
                agreed={a.target_id: a.ready_at for a in measures if a.kind == "order_due"},
            )
            options.append(option.model_copy(update={"actions": measures}))
        except ValueError:
            options.append(
                study.option(
                    kind="treatment",
                    title=title,
                    status="BLOCKED",
                    summary="The object, quantity, availability or execution facts of this measure do not meet the execution conditions, so it cannot be executed.",
                )
            )

    def delay(option: BusinessOption) -> int:
        return sum((i.tardiness_minutes or 0) * i.quantity for i in option.impacts)

    def outlay(option: BusinessOption) -> int:
        return sum(action_cost(action)[1] for action in option.actions)

    # A later date is only worth asking a customer for when no measure keeps every promise, or
    # when it spends less than every measure that does; a faster proposal must deliver sooner.
    kept = [outlay(option) for option in options[:protected] if option.status == "FEASIBLE"]
    result = []
    for index, option in enumerate(options):
        if protected <= index < negotiated:
            dated = any(a.kind == "order_due" for a in option.actions)
            if kept and (option.status != "FEASIBLE" or not dated or outlay(option) >= min(kept)):
                continue
            first = options[protected]
            if index > protected and first.status == "FEASIBLE" and delay(option) >= delay(first):
                continue
        result.append(option)
    return result


def evaluate_business_options(
    snapshot: Snapshot,
    baseline: Candidate | None,
    request: BusinessStudyRequest,
    *,
    expedite_quotes: tuple[ExpediteQuote, ...] = (),
) -> BusinessStudy:
    """Compare isolated scenarios; quote objects must come from trusted source business terms.

    The one bounded wall-clock solve budget (up to 120 seconds for treatments) is shared by all options and search probes.
    A solver model build cannot be interrupted, but no new probe starts after the deadline.
    """
    snapshot = Snapshot.model_validate(snapshot)
    request = BusinessStudyRequest.model_validate(request)
    if len({q.quote_id for q in expedite_quotes}) != len(expedite_quotes):
        raise ValueError("Source quote identifiers must be unique")
    target_order = request.order
    if request.existing_order_id is not None:
        target_order = next(
            (order for order in snapshot.orders if order.order_id == request.existing_order_id),
            None,
        )
        if target_order is None:
            raise ValueError("The selected order is missing from the source snapshot")
    if request.order is not None:
        if any(order.order_id == request.order.order_id for order in snapshot.orders):
            raise ValueError("A promise study must not replace an existing order")
    if target_order is not None:
        product = next(
            (p for p in snapshot.profile.products if p.product_id == target_order.product_id), None
        )
        if product is None or target_order.quantity % product.batch_size:
            raise ValueError(
                "The requested product and complete batch quantity must match source facts"
            )
        if request.final_due_at is not None and request.final_due_at > snapshot.horizon.end_at:
            raise ValueError("A proposed final delivery must stay inside the planning horizon")
        if request.final_due_at is not None and request.final_due_at < target_order.due_at:
            raise ValueError("Final delivery cannot precede the source order's requested delivery")
    study = _Study(snapshot, baseline, request)
    options: list[BusinessOption] = []
    if request.kind == "production_exception":
        options = _treatment_options(study)
    elif request.kind == "urgent_order":
        blocked = (
            _existing_order_block(snapshot, target_order)
            if request.existing_order_id is not None and target_order is not None
            else None
        )
        total = 4 if request.partial_delivery_allowed else 3
        for index, kind in enumerate(
            ("normal", "overtime", "earliest_completion", "partial_delivery")[:total]
        ):
            deadline = time.perf_counter() + study.remaining(study.deadline) / (total - index)
            if blocked is not None:
                option = study.option(
                    kind=kind,
                    title={
                        "normal": "Regular-shift commitment",
                        "overtime": "Overtime commitment",
                        "earliest_completion": "Negotiate a full delivery date",
                        "partial_delivery": "Split delivery as permitted",
                    }[kind],
                    status="BLOCKED",
                    summary=blocked,
                )
            elif kind == "earliest_completion":
                option = study.earliest(deadline)
            elif kind == "partial_delivery":
                option = study.partial(deadline, options[0])
            else:
                option = study.fixed(
                    kind,
                    "Regular-shift commitment" if kind == "normal" else "Overtime commitment",
                    deadline,
                    overtime=kind == "overtime",
                )
            options.append(option)
    else:
        total = 3 + len(request.expedite_quote_ids)
        kinds = ("shared_material", "overtime", "wait_diagnostic", *request.expedite_quote_ids)
        quotes = {quote.quote_id: quote for quote in expedite_quotes}
        for index, kind in enumerate(kinds):
            deadline = time.perf_counter() + study.remaining(study.deadline) / (total - index)
            if index < 3:
                options.append(
                    study.fixed(
                        kind,
                        (
                            "Allocation of existing stock and receipts",
                            "Overtime schedule with existing receipts",
                            "Diagnosis of waiting for receipts",
                        )[index],
                        deadline,
                        overtime=index == 1,
                        diagnostic=index == 2,
                    )
                )
                continue
            quote = quotes.get(kind)
            error = _quote_error(snapshot, request, quote)
            if error is not None:
                options.append(
                    study.option(
                        kind="receipt_expedite",
                        title="Quoted expedited receipt",
                        status="BLOCKED",
                        summary=error,
                        quote=quote,
                    )
                )
            else:
                options.append(
                    study.fixed(
                        "receipt_expedite", "Quoted expedited receipt", deadline, quote=quote
                    )
                )
    options = with_economics(snapshot, options)
    if request.kind == "production_exception":
        options = rank_treatments(options, request, snapshot)
    return BusinessStudy(
        study_id=study.study_id,
        factory_id=snapshot.factory_id,
        run_id=snapshot.run_id,
        origin_snapshot_id=snapshot.snapshot_id,
        origin_snapshot_hash=str(snapshot.content_hash),
        origin_snapshot_clock=snapshot.snapshot_clock,
        baseline_hash=baseline.content_hash if baseline else None,
        request=request,
        options=tuple(options),
        elapsed_seconds=time.perf_counter() - study.started,
    )
