"""Project unchanged approved work onto confirmed execution, without creating a new candidate."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta

from packages.domain.models import (
    ActualExecution,
    Assignment,
    Candidate,
    Snapshot,
    batch_operations,
    canonical_hash,
)
from packages.planning.disruption import recovery_operations

MINUTE = timedelta(minutes=1)


class ProjectionError(ValueError):
    def __init__(self, code: str, object_id: str | None = None):
        self.code, self.object_id = code, object_id
        super().__init__(code)


def remaining_actions(snapshot: Snapshot, assignments: Sequence[Assignment]) -> tuple[dict, ...]:
    completed = {a.operation_id for a in snapshot.actuals if a.state == "COMPLETED"}
    result = []
    for item in sorted(assignments, key=lambda row: row.operation_id):
        if item.operation_id in completed:
            continue
        begin = max(snapshot.snapshot_clock, item.resume_changeover_start or item.changeover_start)
        start = max(begin, item.resume_at or item.start_at)
        result.append(
            {
                "operation_id": item.operation_id,
                "resource_id": item.resource_id,
                "worker_id": item.worker_id,
                "changeover_start": begin.isoformat(),
                "start_at": start.isoformat(),
                "end_at": item.end_at.isoformat(),
            }
        )
    return tuple(result)


def remaining_actions_hash(snapshot: Snapshot, assignments: Sequence[Assignment]) -> str:
    return canonical_hash({"remaining_actions": remaining_actions(snapshot, assignments)})


def _natural_continuation(
    snapshot: Snapshot,
    assignments: Sequence[Assignment],
    *,
    strict: bool = True,
    held: set[str] | None = None,
) -> tuple[Assignment, ...]:
    """Forecast only confirmed actuals; untouched future dispatches retain their approved times."""
    actuals = {row.operation_id: row for row in snapshot.actuals}
    live = {row.operation_id for row in batch_operations(snapshot)[1]}
    cancelled = {
        row.operation_id for row in batch_operations(snapshot, include_cancelled=True)[1]
    } - live
    result = []
    for planned in assignments:
        if planned.operation_id in cancelled:
            continue
        actual = actuals.get(planned.operation_id)
        if actual is None or planned.operation_id in (held or set()):
            result.append(planned)
            continue
        if (
            actual.resource_id != planned.resource_id
            or actual.worker_id != planned.worker_id
            or actual.changeover_start is None
            or not actual.segments
        ):
            if not strict:
                result.append(planned)
                continue
            raise ProjectionError("BASELINE_EXECUTION_CHANGED", actual.operation_id)
        if actual.state == "COMPLETED":
            values = dict(
                start_at=actual.actual_start,
                end_at=actual.actual_end,
                changeover_start=actual.changeover_start,
                resume_at=None,
                resume_changeover_start=None,
            )
        else:
            if (
                actual.state not in {"SETUP", "IN_PROGRESS"}
                or actual.remaining_minutes is None
                or actual.remaining_setup_minutes is None
                or actual.remaining_confirmed_by is None
                or actual.quality_state != "PENDING"
                or actual.completed_quantity != 0
            ):
                if not strict:
                    result.append(planned)
                    continue
                raise ProjectionError("NON_NORMAL_EXECUTION", actual.operation_id)
            production = snapshot.snapshot_clock + actual.remaining_setup_minutes * MINUTE
            values = dict(
                start_at=actual.actual_start or production,
                end_at=production + actual.remaining_minutes * MINUTE,
                changeover_start=actual.changeover_start,
                resume_at=production,
                resume_changeover_start=snapshot.snapshot_clock,
            )
        result.append(Assignment.model_validate({**planned.model_dump(), **values}))
    return tuple(result)


def first_changed_occupancy(
    snapshot: Snapshot, assignments: Sequence[Assignment], baseline: Candidate | None
) -> datetime:
    previous = {
        row["operation_id"]: row
        for row in remaining_actions(
            snapshot,
            _natural_continuation(snapshot, baseline.assignments, strict=False) if baseline else (),
        )
    }
    changed = [
        datetime.fromisoformat(row["changeover_start"])
        for row in remaining_actions(snapshot, assignments)
        if previous.get(row["operation_id"]) != row
    ]
    return min(changed, default=snapshot.horizon.end_at)


def _rows(rows, identity, excluded=()):
    return {getattr(row, identity): row.model_dump(exclude=set(excluded)) for row in rows}


def _history_until(actual: ActualExecution, end: datetime) -> list[dict]:
    return [
        {**segment.model_dump(), "end_at": min(end, segment.end_at)}
        for segment in actual.segments
        if segment.start_at < end
    ]


def _normal_actual(
    original: Snapshot,
    current: Snapshot,
    actual: ActualExecution,
    before: ActualExecution | None,
    planned: Assignment,
) -> None:
    identity = actual.operation_id
    if actual.state not in {"SETUP", "IN_PROGRESS", "COMPLETED"}:
        raise ProjectionError("NON_NORMAL_EXECUTION", identity)
    if before is not None and before.state == "COMPLETED":
        if actual != before:
            raise ProjectionError("ACTUAL_HISTORY_CHANGED", identity)
        return
    if before is not None:
        if (
            before.state not in {"SETUP", "IN_PROGRESS"}
            or actual.version < before.version
            or _history_until(actual, original.snapshot_clock)
            != [segment.model_dump() for segment in before.segments]
            or actual.consumed[: len(before.consumed)] != before.consumed
        ):
            raise ProjectionError("ACTUAL_HISTORY_CHANGED", identity)
    elif (
        actual.changeover_start is None
        or actual.changeover_start < original.snapshot_clock
        or any(segment.start_at < original.snapshot_clock for segment in actual.segments)
    ):
        raise ProjectionError("UNEXPECTED_EXECUTION_HISTORY", identity)
    if (
        actual.resource_id != planned.resource_id
        or actual.worker_id != planned.worker_id
        or actual.changeover_start != planned.changeover_start
    ):
        raise ProjectionError("BASELINE_EXECUTION_CHANGED", identity)
    now = current.snapshot_clock
    production_start = planned.resume_at or planned.start_at
    prefix_begin = max(
        original.snapshot_clock, planned.resume_changeover_start or planned.changeover_start
    )
    prefix_end = min(now, planned.end_at)
    if prefix_begin > prefix_end:
        raise ProjectionError("UNEXPECTED_EXECUTION_HISTORY", identity)
    cursor = prefix_begin
    for segment in actual.segments:
        if segment.end_at <= prefix_begin:
            continue
        begin = max(prefix_begin, segment.start_at)
        if begin != cursor or segment.end_at > prefix_end:
            raise ProjectionError("NON_NORMAL_EXECUTION_SEGMENTS", identity)
        if segment.phase == "SETUP":
            if segment.end_at > production_start:
                raise ProjectionError("NON_NORMAL_EXECUTION_SEGMENTS", identity)
        elif begin < production_start:
            raise ProjectionError("NON_NORMAL_EXECUTION_SEGMENTS", identity)
        cursor = segment.end_at
    if cursor != prefix_end:
        raise ProjectionError("NON_NORMAL_EXECUTION_SEGMENTS", identity)
    expected_state = (
        "COMPLETED"
        if now >= planned.end_at
        else "SETUP"
        if now <= production_start and (before is None or before.actual_start is None)
        else "IN_PROGRESS"
    )
    expected_start = (
        planned.start_at
        if now > production_start or (before is not None and before.actual_start is not None)
        else None
    )
    if (
        actual.state != expected_state
        or actual.actual_start != expected_start
        or actual.actual_end != (planned.end_at if expected_state == "COMPLETED" else None)
        or actual.remaining_minutes is None
        or actual.remaining_setup_minutes is None
        or actual.remaining_confirmed_by is None
        or actual.remaining_minutes * MINUTE
        != max(timedelta(0), planned.end_at - max(now, production_start))
        or actual.remaining_setup_minutes * MINUTE != max(timedelta(0), production_start - now)
        or actual.quality_state != ("PASSED" if expected_state == "COMPLETED" else "PENDING")
    ):
        raise ProjectionError("NON_NORMAL_EXECUTION", identity)


def _transition_facts(original: Snapshot, current: Snapshot) -> None:
    for field in (
        "factory_id",
        "run_id",
        "profile",
        "horizon",
        "scope_version",
        "active_plan_version",
        "active_plan_hash",
        "production_batches",
        "business_terms",
    ):
        if getattr(original, field) != getattr(current, field):
            raise ProjectionError("REVALIDATION_SCOPE_CHANGED", field)
    if (
        current.snapshot_clock < original.snapshot_clock
        or (current.snapshot_clock - original.snapshot_clock) % MINUTE
        or current.planning_revision < original.planning_revision
        or (
            current.snapshot_clock > original.snapshot_clock
            and current.planning_revision <= original.planning_revision
        )
    ):
        raise ProjectionError("REVALIDATION_TIME_CHANGED")
    for field in ("source_system", "ownership", "evidence_digest"):
        if getattr(original.source, field) != getattr(current.source, field):
            raise ProjectionError("REVALIDATION_SOURCE_CHANGED", field)
    if _rows(original.orders, "order_id", ("version", "status")) != _rows(
        current.orders, "order_id", ("version", "status")
    ):
        raise ProjectionError("REVALIDATION_ORDERS_CHANGED")
    if original.receipts != current.receipts or original.workers != current.workers:
        raise ProjectionError("REVALIDATION_FACTS_CHANGED")
    if _rows(
        original.resources, "resource_id", ("version", "last_product_id", "last_operation_id")
    ) != _rows(
        current.resources, "resource_id", ("version", "last_product_id", "last_operation_id")
    ):
        raise ProjectionError("REVALIDATION_RESOURCES_CHANGED")
    before_stock = {row.material_id: row for row in original.inventory}
    if set(before_stock) != {row.material_id for row in current.inventory}:
        raise ProjectionError("REVALIDATION_INVENTORY_CHANGED")
    consumed: dict[str, int] = defaultdict(int)
    reserved: dict[str, int] = defaultdict(int)
    for facts, sign in ((original, -1), (current, 1)):
        for actual in facts.actuals:
            for item in actual.consumed:
                consumed[item.material_id] += sign * item.quantity
        for reservation in facts.reservations:
            reserved[reservation.material_id] += sign * reservation.quantity
    for stock in current.inventory:
        before = before_stock[stock.material_id]
        if (
            stock.unit != before.unit
            or stock.on_hand != before.on_hand - consumed[stock.material_id]
            or stock.reserved != before.reserved + reserved[stock.material_id]
        ):
            raise ProjectionError("REVALIDATION_INVENTORY_CHANGED", stock.material_id)
    old_reservations = {row.reservation_id: row for row in original.reservations}
    if not set(old_reservations) <= {row.reservation_id for row in current.reservations}:
        raise ProjectionError("RESERVATION_HISTORY_CHANGED")
    for reservation in current.reservations:
        prior_reservation = old_reservations.get(reservation.reservation_id)
        if prior_reservation is not None:
            if prior_reservation.model_dump(exclude={"quantity"}) != reservation.model_dump(
                exclude={"quantity"}
            ):
                raise ProjectionError("RESERVATION_HISTORY_CHANGED", reservation.reservation_id)
        else:
            starts = [
                a.actual_start
                for a in current.actuals
                if a.batch_id == reservation.batch_id and a.actual_start is not None
            ]
            if (
                not starts
                or reservation.created_at != min(starts)
                or reservation.plan_version != current.active_plan_version
            ):
                raise ProjectionError("RESERVATION_HISTORY_CHANGED", reservation.reservation_id)


def project_execution(
    original: Snapshot, current: Snapshot, candidate: Candidate, *, baseline: Candidate
) -> tuple[Assignment, ...]:
    _transition_facts(original, current)
    all_old_actuals = {row.operation_id: row for row in original.actuals}
    all_actuals = {row.operation_id: row for row in current.actuals}
    if not set(all_old_actuals) <= set(all_actuals):
        raise ProjectionError("ACTUAL_HISTORY_CHANGED")
    live = {row.operation_id for row in batch_operations(original)[1]}
    for identity in set(all_old_actuals) - live:
        if all_actuals[identity] != all_old_actuals[identity]:
            raise ProjectionError("ACTUAL_HISTORY_CHANGED", identity)
    old_actuals = {identity: row for identity, row in all_old_actuals.items() if identity in live}
    actuals = {identity: row for identity, row in all_actuals.items() if identity in live}
    approved = {row.operation_id: row for row in candidate.assignments}
    original_prior = {row.operation_id: row for row in baseline.assignments}
    recovery = recovery_operations(original, baseline)
    held = {
        identity
        for identity in recovery
        if identity in old_actuals and old_actuals[identity].state == "BLOCKED"
    }
    continuation = _natural_continuation(original, baseline.assignments, held=held)
    prior = {row.operation_id: row for row in continuation}
    if not set(actuals) <= set(approved) or not set(actuals) <= set(prior):
        raise ProjectionError("UNEXPECTED_EXECUTION_HISTORY")
    prior_future = {row["operation_id"]: row for row in remaining_actions(original, continuation)}
    approved_future = {
        row["operation_id"]: row for row in remaining_actions(original, candidate.assignments)
    }
    for identity, row in prior_future.items():
        if (
            datetime.fromisoformat(row["changeover_start"]) < current.snapshot_clock
            and identity not in actuals
            and identity not in recovery
            and (
                original_prior[identity].resume_changeover_start
                or original_prior[identity].changeover_start
            )
            >= original.snapshot_clock
        ):
            raise ProjectionError("EXPECTED_PREFIX_MISSING", identity)
    for identity, actual in actuals.items():
        before = old_actuals.get(identity)
        if identity in held:
            if actual != before:
                raise ProjectionError("BLOCKED_EXECUTION_CHANGED", identity)
            continue
        if before is None or actual != before:
            if prior_future.get(identity) != approved_future.get(identity):
                raise ProjectionError("APPROVED_PREFIX_CHANGED", identity)
        _normal_actual(original, current, actual, before, prior[identity])
    batches, operations = batch_operations(current)
    batch_by_id = {batch.batch_id: batch for batch in batches}
    for order in current.orders:
        expected = [
            operation.operation_id
            for operation in operations
            if batch_by_id[operation.batch_id].order_id == order.order_id
            and not any(
                batch.batch_id == operation.batch_id and batch.purpose != "CUSTOMER"
                for batch in current.production_batches or ()
            )
        ]
        all_complete = all(
            identity in actuals and actuals[identity].state == "COMPLETED" for identity in expected
        )
        any_started = any(
            identity in actuals and actuals[identity].actual_start is not None
            for identity in expected
        )
        prior_order = next(item for item in original.orders if item.order_id == order.order_id)
        state = (
            "CANCELLED"
            if prior_order.status == "CANCELLED"
            else "COMPLETED"
            if all_complete
            else "IN_PROGRESS"
            if any_started
            else prior_order.status
        )
        if order.status != state or order.version < prior_order.version:
            raise ProjectionError("NON_NORMAL_ORDER_PROGRESS", order.order_id)
    for resource in current.resources:
        before_resource = next(
            item for item in original.resources if item.resource_id == resource.resource_id
        )
        started = [
            actual
            for identity, actual in actuals.items()
            if identity not in old_actuals and actual.resource_id == resource.resource_id
        ]
        pointer: tuple[str | None, str | None]
        if started:
            latest = max(started, key=lambda item: item.changeover_start or original.snapshot_clock)
            pointer = latest.operation_id, batch_by_id[latest.batch_id].product_id
        else:
            pointer = before_resource.last_operation_id, before_resource.last_product_id
        if (resource.last_operation_id, resource.last_product_id) != pointer:
            raise ProjectionError("NON_NORMAL_RESOURCE_SETUP", resource.resource_id)
    result = []
    for item in candidate.assignments:
        observed = actuals.get(item.operation_id)
        if observed is None or item.operation_id in held:
            result.append(item)
            continue
        data = item.model_dump()
        if observed.state == "COMPLETED":
            data.update(
                start_at=observed.actual_start,
                end_at=observed.actual_end,
                changeover_start=observed.changeover_start,
                resource_id=observed.resource_id,
                worker_id=observed.worker_id,
                resume_at=None,
                resume_changeover_start=None,
            )
        else:
            assert observed.remaining_setup_minutes is not None
            production = current.snapshot_clock + observed.remaining_setup_minutes * MINUTE
            data.update(
                start_at=observed.actual_start or production,
                changeover_start=observed.changeover_start,
                resume_at=production,
                resume_changeover_start=current.snapshot_clock,
            )
        result.append(Assignment.model_validate(data))
    return tuple(result)
