"""Prove exact baseline progress from complete, hash-anchored public source revisions."""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from packages.domain.models import (
    ActualExecution,
    Assignment,
    Candidate,
    Event,
    Snapshot,
    batch_operations,
    canonical_hash,
)
from packages.domain.snapshot_delta import apply_delta

MINUTE = timedelta(minutes=1)
MAX_PROGRESS_REVISIONS = 1000
COLLECTION_KEYS = {
    "orders": "order_id",
    "inventory": "material_id",
    "receipts": "receipt_id",
    "resources": "resource_id",
    "workers": "worker_id",
    "actuals": "operation_id",
}


class ProgressEvidenceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise ProgressEvidenceError(code)


def _revision(value: Any) -> int:
    _need(
        isinstance(value, str) and value.isascii() and value.isdecimal(), "INVALID_SOURCE_REVISION"
    )
    result = int(value)
    _need(str(result) == value, "INVALID_SOURCE_REVISION")
    return result


def _minutes(value: timedelta) -> int:
    _need(value >= timedelta(0) and value % MINUTE == timedelta(0), "UNPROVEN_PROGRESS_TIME")
    return value // MINUTE


def _rows(snapshot: Snapshot, collection: str, identity: str) -> dict[str, dict]:
    return {
        getattr(row, identity): row.model_dump(mode="json") for row in getattr(snapshot, collection)
    }


def _current(snapshot: Snapshot, held: dict[str, ActualExecution] | None = None) -> None:
    _need(
        snapshot.schema_version in {"byof.snapshot/2", "byof.snapshot/3"}
        and snapshot.source.complete
        and snapshot.source.freshness == "CURRENT"
        and snapshot.source.consistency != "UNVERIFIED",
        "INCOMPLETE_PROGRESS_FACTS",
    )
    _need(snapshot.source.source_system != "factory-simulator-replay", "REPLAY_NOT_ALLOWED")
    live = {operation.operation_id for operation in batch_operations(snapshot)[1]}
    _need(
        all(
            (actual.state != "BLOCKED" or (held or {}).get(actual.operation_id) == actual)
            and actual.quality_state not in {"UNKNOWN", "FAILED"}
            for actual in snapshot.actuals
            if actual.operation_id in live
        )
        and all(resource.status == "AVAILABLE" for resource in snapshot.resources)
        and all(worker.status == "AVAILABLE" for worker in snapshot.workers),
        "UNRESOLVED_PROGRESS_FACT",
    )


def _execution_anchor(
    original: Snapshot, baseline: Candidate, held: dict[str, ActualExecution] | None = None
) -> tuple[Assignment, ...]:
    """Confirmed source work is the forecast origin; an obsolete plan is not actual history."""
    live = {row.operation_id for row in batch_operations(original)[1]}
    actuals = {row.operation_id: row for row in original.actuals if row.operation_id in live}
    cancelled = {
        row.operation_id for row in batch_operations(original, include_cancelled=True)[1]
    } - live
    _need(
        actuals.keys() <= {row.operation_id for row in baseline.assignments}, "UNPLANNED_EXECUTION"
    )
    result = []
    for assignment in baseline.assignments:
        if assignment.operation_id in cancelled:
            continue
        actual = actuals.get(assignment.operation_id)
        if actual is None or (held or {}).get(assignment.operation_id) == actual:
            result.append(assignment)
            continue
        _need(
            actual.resource_id == assignment.resource_id
            and actual.worker_id == assignment.worker_id,
            "BASELINE_HISTORY_CHANGED",
        )
        _need(
            actual.changeover_start is not None
            and actual.changeover_start < original.snapshot_clock
            and bool(actual.segments),
            "UNPROVEN_EXECUTION_START",
        )
        if actual.state == "COMPLETED":
            _need(
                actual.actual_start is not None
                and actual.actual_end is not None
                and actual.quality_state == "PASSED"
                and actual.remaining_minutes == actual.remaining_setup_minutes == 0,
                "UNPROVEN_COMPLETION",
            )
            values = dict(
                start_at=actual.actual_start,
                end_at=actual.actual_end,
                changeover_start=actual.changeover_start,
                resume_at=None,
                resume_changeover_start=None,
            )
        else:
            _need(
                actual.state in {"SETUP", "IN_PROGRESS"}
                and actual.quality_state == "PENDING"
                and actual.completed_quantity == 0,
                "UNPROVEN_PROGRESS_STATE",
            )
            _need(
                actual.remaining_minutes is not None
                and actual.remaining_setup_minutes is not None
                and actual.remaining_confirmed_by is not None,
                "UNPROVEN_REMAINING_WORK",
            )
            assert actual.remaining_minutes is not None
            assert actual.remaining_setup_minutes is not None
            production = original.snapshot_clock + actual.remaining_setup_minutes * MINUTE
            values = dict(
                start_at=actual.actual_start or production,
                end_at=production + actual.remaining_minutes * MINUTE,
                changeover_start=actual.changeover_start,
                resume_at=production,
                resume_changeover_start=original.snapshot_clock,
            )
        result.append(Assignment.model_validate({**assignment.model_dump(), **values}))
    return tuple(result)


def _planned_actuals(
    snapshot: Snapshot,
    assignments: Sequence[Assignment],
    missed: set[str],
    held: dict[str, ActualExecution] | None = None,
) -> None:
    actuals = {row.operation_id: row for row in snapshot.actuals}
    for assignment in assignments:
        occupancy = assignment.resume_changeover_start or assignment.changeover_start
        production = assignment.resume_at or assignment.start_at
        actual = actuals.get(assignment.operation_id)
        if held is not None and assignment.operation_id in held:
            _need(actual == held[assignment.operation_id], "BLOCKED_EXECUTION_CHANGED")
            continue
        if actual is None and (
            occupancy >= snapshot.snapshot_clock or assignment.operation_id in missed
        ):
            continue
        _need(actual is not None, "BASELINE_PROGRESS_MISSING")
        assert actual is not None
        _need(
            actual.changeover_start is not None
            and actual.changeover_start < snapshot.snapshot_clock
            and bool(actual.segments),
            "UNPROVEN_EXECUTION_START",
        )
        _need(
            actual.resource_id == assignment.resource_id
            and actual.worker_id == assignment.worker_id
            and actual.changeover_start == assignment.changeover_start,
            "BASELINE_HISTORY_CHANGED",
        )
        if actual.actual_start is not None:
            _need(actual.actual_start == assignment.start_at, "BASELINE_HISTORY_CHANGED")
        if snapshot.snapshot_clock >= assignment.end_at:
            _need(
                actual.state == "COMPLETED"
                and actual.actual_end == assignment.end_at
                and actual.quality_state == "PASSED"
                and actual.remaining_minutes == 0
                and actual.remaining_setup_minutes == 0,
                "UNPROVEN_COMPLETION",
            )
        else:
            _need(
                actual.state in {"SETUP", "IN_PROGRESS"}
                and actual.quality_state == "PENDING"
                and actual.completed_quantity == 0,
                "UNPROVEN_PROGRESS_STATE",
            )
            _need(
                actual.remaining_setup_minutes
                == _minutes(max(production - snapshot.snapshot_clock, timedelta(0)))
                and actual.remaining_minutes
                == _minutes(assignment.end_at - max(production, snapshot.snapshot_clock)),
                "UNPROVEN_REMAINING_WORK",
            )
            _need(
                (actual.actual_start is not None)
                == (
                    snapshot.snapshot_clock > production
                    or assignment.resume_at is not None
                    and assignment.start_at < production
                ),
                "UNPROVEN_PRODUCTION_START",
            )


def _new_segments(
    before: ActualExecution | None, after: ActualExecution
) -> list[tuple[str, datetime, datetime]]:
    previous = before.segments if before else ()
    _need(len(after.segments) >= len(previous), "EXECUTION_HISTORY_CHANGED")
    added: list[tuple[str, datetime, datetime]] = []
    for index, segment in enumerate(previous):
        current = after.segments[index]
        if index != len(previous) - 1:
            _need(current == segment, "EXECUTION_HISTORY_CHANGED")
        else:
            _need(
                current.phase == segment.phase
                and current.source_event_id == segment.source_event_id
                and current.start_at == segment.start_at
                and current.end_at >= segment.end_at,
                "EXECUTION_HISTORY_CHANGED",
            )
            if current.end_at > segment.end_at:
                added.append((current.phase, segment.end_at, current.end_at))
    added.extend(
        (segment.phase, segment.start_at, segment.end_at)
        for segment in after.segments[len(previous) :]
    )
    return added


def _step(
    before: Snapshot,
    after: Snapshot,
    assignments: Sequence[Assignment],
    missed: set[str],
    held: dict[str, ActualExecution] | None = None,
) -> None:
    _current(after, held)
    _need(after.snapshot_clock - before.snapshot_clock == MINUTE, "UNPROVEN_PROGRESS_TIME")
    _need(after.planning_revision == before.planning_revision + 1, "PROGRESS_VERSION_GAP")
    for field in (
        "factory_id",
        "run_id",
        "schema_version",
        "horizon",
        "scope_version",
        "profile",
        "active_plan_version",
        "active_plan_hash",
        "receipts",
        "workers",
        "production_batches",
        "business_terms",
    ):
        _need(getattr(after, field) == getattr(before, field), "MATERIAL_PROGRESS_CHANGE")
    old_source = before.source.model_dump(
        exclude={"source_revision", "cursor", "observed_at", "effective_at"}
    )
    new_source = after.source.model_dump(
        exclude={"source_revision", "cursor", "observed_at", "effective_at"}
    )
    _need(old_source == new_source, "SOURCE_CONTRACT_CHANGED")
    _need(after.source.effective_at == after.snapshot_clock, "SOURCE_CLOCK_MISMATCH")
    _need(after.source.cursor in (None, after.source.source_revision), "SOURCE_CURSOR_MISMATCH")
    _planned_actuals(after, assignments, missed, held)
    all_old_actuals = {actual.operation_id: actual for actual in before.actuals}
    all_new_actuals = {actual.operation_id: actual for actual in after.actuals}
    _need(all_old_actuals.keys() <= all_new_actuals.keys(), "EXECUTION_HISTORY_CHANGED")
    live = {operation.operation_id for operation in batch_operations(after)[1]}
    for identity in set(all_old_actuals) - live:
        _need(all_old_actuals[identity] == all_new_actuals[identity], "EXECUTION_HISTORY_CHANGED")
    old_actuals = {identity: row for identity, row in all_old_actuals.items() if identity in live}
    new_actuals = {identity: row for identity, row in all_new_actuals.items() if identity in live}
    planned = {assignment.operation_id: assignment for assignment in assignments}
    _need(new_actuals.keys() <= planned.keys(), "UNPLANNED_EXECUTION")
    consumed: dict[tuple[str, str], int] = defaultdict(int)
    consumption_counts: dict[str, int] = defaultdict(int)
    new_starts = []
    new_setups = []
    for identity, actual in new_actuals.items():
        previous = old_actuals.get(identity)
        if held is not None and identity in held:
            _need(actual == previous == held[identity], "BLOCKED_EXECUTION_CHANGED")
            continue
        assignment = planned[identity]
        if previous is not None:
            _need(
                actual.consumed[: len(previous.consumed)] == previous.consumed,
                "CONSUMPTION_HISTORY_CHANGED",
            )
            for field in (
                "batch_id",
                "route_version",
                "resource_id",
                "worker_id",
                "changeover_start",
            ):
                _need(
                    getattr(actual, field) == getattr(previous, field), "EXECUTION_HISTORY_CHANGED"
                )
            if previous.actual_start is not None:
                _need(
                    actual.actual_start == previous.actual_start
                    and actual.consumed == previous.consumed,
                    "CONSUMPTION_HISTORY_CHANGED",
                )
            if previous.state == "COMPLETED":
                _need(actual == previous, "EXECUTION_HISTORY_CHANGED")
            elif actual != previous:
                _need(actual.version == previous.version + 1, "PROGRESS_ENTITY_VERSION_CHANGED")
        else:
            _need(actual.version >= 1, "PROGRESS_ENTITY_VERSION_CHANGED")
            new_setups.append(actual)
        occupancy = assignment.resume_changeover_start or assignment.changeover_start
        production = assignment.resume_at or assignment.start_at
        if previous is None:
            _need(
                before.snapshot_clock <= occupancy < after.snapshot_clock,
                "UNPROVEN_EXECUTION_START",
            )
        expected = []
        for phase, begin, end in (
            ("SETUP", occupancy, production),
            ("PRODUCTION", production, assignment.end_at),
        ):
            begin, end = max(begin, before.snapshot_clock), min(end, after.snapshot_clock)
            if begin < end:
                expected.append((phase, begin, end))
        _need(_new_segments(previous, actual) == expected, "UNPROVEN_EXECUTION_SEGMENTS")
        if actual.actual_start is not None and (previous is None or previous.actual_start is None):
            _need(
                before.snapshot_clock <= actual.actual_start < after.snapshot_clock,
                "UNPROVEN_PRODUCTION_START",
            )
            new_starts.append(actual)
        previous_count = len(previous.consumed) if previous else 0
        added = actual.consumed[previous_count:]
        _need(not added or actual in new_starts, "EXTRA_CONSUMPTION")
        for item in added:
            consumed[actual.batch_id, item.material_id] += item.quantity
            consumption_counts[item.material_id] += 1

    batches, operations = batch_operations(after)
    by_batch = {batch.batch_id: batch for batch in batches}
    old_started = {actual.batch_id for actual in before.actuals if actual.actual_start is not None}
    new_batches = {actual.batch_id for actual in new_starts} - old_started
    kits: dict[str, int] = defaultdict(int)
    kit_counts: dict[str, int] = defaultdict(int)
    expected_new_reservations = set()
    for batch_id in new_batches:
        batch = by_batch[batch_id]
        for bom in after.profile.bom:
            if bom.product_id == batch.product_id:
                kits[bom.material_id] += bom.quantity_per_unit * batch.quantity
                kit_counts[bom.material_id] += 1
                expected_new_reservations.add((batch_id, bom.material_id))
    old_reserved = {(row.batch_id, row.material_id): row for row in before.reservations}
    new_reserved = {(row.batch_id, row.material_id): row for row in after.reservations}
    _need(old_reserved.keys() <= new_reserved.keys(), "RESERVATION_HISTORY_CHANGED")
    _need(
        new_reserved.keys() - old_reserved.keys() == expected_new_reservations, "EXTRA_RESERVATION"
    )
    for key, reservation in new_reserved.items():
        old = old_reserved.get(key)
        if old:
            _need(
                reservation.model_dump(exclude={"quantity"})
                == old.model_dump(exclude={"quantity"}),
                "RESERVATION_HISTORY_CHANGED",
            )
            _need(
                reservation.quantity == old.quantity - consumed[key], "UNPROVEN_RESERVATION_BALANCE"
            )
        else:
            _need(reservation.plan_version == after.active_plan_version, "RESERVATION_PLAN_CHANGED")
            production_starts = [
                actual.actual_start
                for actual in new_starts
                if actual.batch_id == reservation.batch_id and actual.actual_start is not None
            ]
            _need(bool(production_starts), "RESERVATION_HISTORY_CHANGED")
            _need(reservation.created_at == min(production_starts), "RESERVATION_HISTORY_CHANGED")
    old_inventory = {row.material_id: row for row in before.inventory}
    _need(
        old_inventory.keys() == {row.material_id for row in after.inventory},
        "MATERIAL_SCOPE_CHANGED",
    )
    for stock in after.inventory:
        old_stock = old_inventory[stock.material_id]
        used = sum(
            value for (_, material), value in consumed.items() if material == stock.material_id
        )
        _need(
            kits[stock.material_id] <= old_stock.on_hand - old_stock.reserved, "UNPROVEN_FULL_KIT"
        )
        _need(
            stock.unit == old_stock.unit
            and stock.on_hand == old_stock.on_hand - used
            and stock.reserved == old_stock.reserved + kits[stock.material_id] - used
            and stock.version
            == old_stock.version
            + kit_counts[stock.material_id]
            + consumption_counts[stock.material_id],
            "UNPROVEN_INVENTORY_BALANCE",
        )
    old_orders = {order.order_id: order for order in before.orders}
    _need(old_orders.keys() == {order.order_id for order in after.orders}, "ORDER_SCOPE_CHANGED")
    for order in after.orders:
        old_order = old_orders[order.order_id]
        _need(
            order.model_dump(exclude={"version", "status"})
            == old_order.model_dump(exclude={"version", "status"}),
            "ORDER_CHANGED",
        )
        required = [
            operation.operation_id
            for operation in operations
            if by_batch[operation.batch_id].order_id == order.order_id
            and not any(
                batch.batch_id == operation.batch_id and batch.purpose != "CUSTOMER"
                for batch in after.production_batches or ()
            )
        ]
        complete = all(
            identity in new_actuals and new_actuals[identity].state == "COMPLETED"
            for identity in required
        )
        started = any(
            identity in new_actuals and new_actuals[identity].actual_start is not None
            for identity in required
        )
        status = (
            "CANCELLED"
            if old_order.status == "CANCELLED"
            else "COMPLETED"
            if complete
            else "IN_PROGRESS"
            if started
            else old_order.status
        )
        _need(
            order.status == status
            and order.version == old_order.version + int(status != old_order.status),
            "UNPROVEN_ORDER_PROGRESS",
        )
    old_resources = {resource.resource_id: resource for resource in before.resources}
    _need(
        old_resources.keys() == {resource.resource_id for resource in after.resources},
        "RESOURCE_SCOPE_CHANGED",
    )
    for resource in after.resources:
        old_resource = old_resources[resource.resource_id]
        _need(
            resource.model_dump(exclude={"last_operation_id", "last_product_id", "version"})
            == old_resource.model_dump(exclude={"last_operation_id", "last_product_id", "version"}),
            "RESOURCE_CHANGED",
        )
        setup_starts = sorted(
            (actual for actual in new_setups if actual.resource_id == resource.resource_id),
            key=lambda actual: actual.changeover_start or before.snapshot_clock,
        )
        if setup_starts:
            last = setup_starts[-1]
            _need(
                resource.last_operation_id == last.operation_id
                and resource.last_product_id == by_batch[last.batch_id].product_id
                and resource.version == old_resource.version + len(setup_starts),
                "UNPROVEN_RESOURCE_SETUP",
            )
        else:
            _need(resource == old_resource, "UNPROVEN_RESOURCE_SETUP")


def _events_match(before: Snapshot, after: Snapshot, raw_events: Any) -> None:
    _need(isinstance(raw_events, list), "INCOMPLETE_PROGRESS_EVENTS")
    events = [Event.model_validate(value) for value in raw_events]
    _need(len({event.event_id for event in events}) == len(events), "DUPLICATE_PROGRESS_EVENT")
    _need(
        len({event.source_event_id for event in events}) == len(events), "DUPLICATE_PROGRESS_EVENT"
    )
    by_row = {(event.entity_type, event.entity_id): event for event in events}
    _need(len(by_row) == len(events), "DUPLICATE_PROGRESS_EVENT")
    expected_rows = set()
    for collection, key in COLLECTION_KEYS.items():
        old, new = _rows(before, collection, key), _rows(after, collection, key)
        for identity, row in new.items():
            previous = old.get(identity, {})
            changes = {
                field: (previous.get(field), value)
                for field, value in row.items()
                if field != key
                and value != previous.get(field)
                and not isinstance(value, (list, dict))
            }
            if not changes:
                continue
            expected_rows.add((collection, identity))
            event = by_row.get((collection, identity))
            _need(event is not None, "MISSING_PROGRESS_EVENT")
            assert event is not None
            _need(
                event.factory_id == after.factory_id
                and event.run_id == after.run_id
                and event.source_revision == after.source.source_revision
                and event.entity_version == row["version"]
                and event.corrects_event_id is None
                and event.occurred_at == after.snapshot_clock
                and event.effective_at == after.snapshot_clock
                and event.observed_at == after.source.observed_at,
                "PROGRESS_EVENT_MISMATCH",
            )
            _need(
                len({change.field for change in event.changes}) == len(event.changes),
                "PROGRESS_EVENT_MISMATCH",
            )
            _need(
                {change.field: (change.before, change.after) for change in event.changes}
                == changes,
                "PROGRESS_EVENT_MISMATCH",
            )
            expected_type = "clock.tick"
            if collection == "actuals":
                expected_type = (
                    "execution.progress"
                    if previous.get("state") == row["state"]
                    else "execution." + row["state"].lower()
                )
            _need(event.event_type == expected_type, "MATERIAL_PROGRESS_EVENT")
    _need(set(by_row) == expected_rows, "UNEXPECTED_PROGRESS_EVENT")


def validate_progress_chain(
    original: Snapshot,
    current: Snapshot,
    batches: Sequence[dict],
    *,
    baseline: Candidate,
    candidate: Candidate,
) -> str:
    """Check public source evidence only; permission and objective-head checks belong to services."""
    try:
        original, current = Snapshot.model_validate(original), Snapshot.model_validate(current)
        baseline, candidate = (
            Candidate.model_validate(baseline),
            Candidate.model_validate(candidate),
        )
        from packages.planning.disruption import recovery_operations

        recovery = (
            recovery_operations(original, baseline)
            if candidate.new_actions_not_before is not None
            else set()
        )
        held = {
            a.operation_id: a
            for a in original.actuals
            if a.operation_id in recovery and a.state == "BLOCKED"
        }
        _current(original, held)
        _current(current, held)
        _need(
            (original.factory_id, original.run_id) == (current.factory_id, current.run_id),
            "PROGRESS_SCOPE_CHANGED",
        )
        _need(
            baseline.factory_id == original.factory_id
            and baseline.has_solution
            and original.active_plan_hash == baseline.content_hash
            and current.active_plan_hash == baseline.content_hash
            and original.active_plan_version is not None
            and original.active_plan_version == current.active_plan_version,
            "PROGRESS_BASELINE_CHANGED",
        )
        binding = candidate.binding
        _need(
            candidate.factory_id == original.factory_id
            and candidate.has_solution
            and binding.snapshot_hash == original.content_hash
            and binding.planning_revision == original.planning_revision
            and binding.scope_version == original.scope_version
            and binding.profile_version == original.profile.version
            and binding.policy_version == original.profile.policy.policy_version
            and binding.baseline_plan_version == original.active_plan_version,
            "PROGRESS_CANDIDATE_MISMATCH",
        )
        _, operations = batch_operations(original)
        required = {operation.operation_id for operation in operations}
        recorded = {
            operation.operation_id
            for operation in batch_operations(original, include_cancelled=True)[1]
        }
        _need(
            {assignment.operation_id for assignment in baseline.assignments} <= recorded,
            "BASELINE_SCOPE_MISMATCH",
        )
        _need(
            {assignment.operation_id for assignment in candidate.assignments} == required,
            "CANDIDATE_SCOPE_MISMATCH",
        )
        assignments = _execution_anchor(original, baseline, held)
        actual_ids = {row.operation_id for row in original.actuals}
        missed = {
            row.operation_id
            for row in baseline.assignments
            if row.operation_id not in actual_ids
            and (row.resume_changeover_start or row.changeover_start) < original.snapshot_clock
        }
        missed |= recovery
        _planned_actuals(original, assignments, missed, held)
        first, last = (
            _revision(original.source.source_revision),
            _revision(current.source.source_revision),
        )
        _need(
            0 < last - first == len(batches) <= MAX_PROGRESS_REVISIONS, "INCOMPLETE_PROGRESS_CHAIN"
        )
        anchor = original
        digests = []
        for revision, batch in enumerate(batches, first + 1):
            _need(isinstance(batch, dict), "INVALID_PROGRESS_BATCH")
            _need(
                set(batch)
                == {
                    "factory_id",
                    "run_id",
                    "revision",
                    "previous_snapshot_hash",
                    "snapshot_hash",
                    "business_clock",
                    "cause",
                    "events",
                    "snapshot_delta",
                },
                "INVALID_PROGRESS_BATCH",
            )
            _need(
                batch["factory_id"] == original.factory_id and batch["run_id"] == original.run_id,
                "PROGRESS_SCOPE_CHANGED",
            )
            _need(
                _revision(batch["revision"]) == revision
                and batch["previous_snapshot_hash"] == anchor.content_hash,
                "INCOMPLETE_PROGRESS_CHAIN",
            )
            _need(batch["cause"] == "clock.tick", "MATERIAL_PROGRESS_EVENT")
            after = apply_delta(anchor, batch["snapshot_delta"])
            _need(
                after.content_hash == batch["snapshot_hash"]
                and _revision(after.source.source_revision) == revision,
                "PROGRESS_HASH_MISMATCH",
            )
            _need(
                after.snapshot_clock.isoformat() == batch["business_clock"], "SOURCE_CLOCK_MISMATCH"
            )
            _step(anchor, after, assignments, missed, held)
            _events_match(anchor, after, batch["events"])
            digests.append(canonical_hash(batch))
            anchor = after
        _need(anchor.content_hash == current.content_hash, "PROGRESS_HASH_MISMATCH")
        return canonical_hash(
            {
                "schema_version": "byof.progress-evidence/1",
                "factory_id": original.factory_id,
                "run_id": original.run_id,
                "old_snapshot_hash": original.content_hash,
                "new_snapshot_hash": current.content_hash,
                "baseline_hash": baseline.content_hash,
                "baseline_version": original.active_plan_version,
                "candidate_hash": candidate.content_hash,
                "revisions": digests,
            }
        )
    except ProgressEvidenceError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ProgressEvidenceError("INVALID_PROGRESS_EVIDENCE") from exc
