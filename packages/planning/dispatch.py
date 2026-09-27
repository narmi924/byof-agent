"""Deterministic legal list scheduling used only as a CP-SAT solution hint."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from packages.domain.models import (
    Assignment,
    Batch,
    CalendarWindow,
    Candidate,
    Operation,
    Snapshot,
    TimeWindow,
    batch_operations,
    duration_minutes,
    minute_offset,
)
from packages.planning.disruption import recovery_operations


def working_windows(
    snapshot: Snapshot,
    calendar: tuple[CalendarWindow, ...],
    unavailable: tuple[TimeWindow, ...],
    *,
    overtime: bool,
) -> list[tuple[int, int]]:
    origin = snapshot.horizon.start_at
    horizon = minute_offset(origin, snapshot.horizon.end_at, round_up=False)
    windows: list[tuple[int, int]] = []
    for window in sorted(calendar, key=lambda w: w.start_at):
        if window.kind == "OVERTIME" and not overtime:
            continue
        start = max(0, minute_offset(origin, window.start_at, round_up=True))
        end = min(horizon, minute_offset(origin, window.end_at, round_up=False))
        if start >= end:
            continue
        if windows and windows[-1][1] == start:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    for blocked in unavailable:
        left = minute_offset(origin, blocked.start_at, round_up=False)
        right = minute_offset(origin, blocked.end_at, round_up=True)
        segments = []
        for start, end in windows:
            if right <= start or left >= end:
                segments.append((start, end))
            else:
                if start < left:
                    segments.append((start, left))
                if right < end:
                    segments.append((right, end))
        windows = segments
    return windows


def _first_common_slot(left, right, earliest: int, size: int) -> int | None:
    for a, b in left:
        if b < earliest + size:
            continue
        for c, d in right:
            start = max(a, c, earliest)
            if start + size <= min(b, d):
                return start
    return None


def dispatch(
    snapshot: Snapshot,
    *,
    allow_overtime: bool = False,
    new_actions_not_before: datetime | None = None,
) -> tuple[Assignment, ...]:
    """Return a complete hint or no hint; this never proves infeasibility or optimality."""
    if snapshot.actuals or snapshot.active_plan_version:
        raise ValueError("UNSUPPORTED_WIP_OR_BASELINE: dispatch requires an unstarted snapshot")
    batches, operations = batch_operations(snapshot)
    if any(order.status != "CONFIRMED" for order in snapshot.orders):
        raise ValueError("UNSUPPORTED_ORDER_STATE: dispatch requires confirmed unstarted orders")
    return _list_schedule(
        snapshot,
        batches,
        operations,
        allow_overtime=allow_overtime,
        start_not_before=new_actions_not_before,
    )


def continuation_dispatch(
    snapshot: Snapshot,
    kept: tuple[Assignment, ...],
    *,
    new_actions_not_before: datetime,
    allow_overtime: bool = False,
) -> tuple[Assignment, ...]:
    """Complete a work-in-progress hint: keep known positions and place new operations after them."""
    batches, operations = batch_operations(snapshot)
    started = frozenset(
        operation.batch_id
        for operation in operations
        if any(actual.operation_id == operation.operation_id for actual in snapshot.actuals)
    )
    return _list_schedule(
        snapshot,
        batches,
        operations,
        allow_overtime=allow_overtime,
        locked=kept,
        start_not_before=new_actions_not_before,
        started_batches=started,
    )


def recovery_dispatch(
    snapshot: Snapshot,
    baseline: Candidate,
    *,
    new_actions_not_before: datetime,
    allow_overtime: bool = False,
) -> tuple[Assignment, ...]:
    """Build a complete zero-WIP search hint while retaining protected old dispatches."""
    if snapshot.actuals or snapshot.active_plan_version is None:
        return ()
    if any(order.status != "CONFIRMED" for order in snapshot.orders):
        return ()
    batches, operations = batch_operations(snapshot)
    live = {operation.operation_id for operation in operations}
    recovery = recovery_operations(snapshot, baseline)
    freeze_end = snapshot.snapshot_clock + timedelta(
        minutes=snapshot.profile.policy.freeze_window_min
    )
    locked = tuple(
        assignment
        for assignment in baseline.assignments
        if assignment.operation_id in live
        and assignment.operation_id not in recovery
        and (
            snapshot.snapshot_clock <= assignment.changeover_start < new_actions_not_before
            or snapshot.snapshot_clock <= assignment.start_at < freeze_end
        )
    )
    return _list_schedule(
        snapshot,
        batches,
        operations,
        allow_overtime=allow_overtime,
        locked=locked,
        start_not_before=new_actions_not_before,
    )


def _list_schedule(
    snapshot: Snapshot,
    batches: tuple[Batch, ...],
    operations: tuple[Operation, ...],
    *,
    allow_overtime: bool,
    locked: tuple[Assignment, ...] = (),
    start_not_before: datetime | None = None,
    started_batches: frozenset[str] = frozenset(),
) -> tuple[Assignment, ...]:
    origin = snapshot.horizon.start_at
    now = max(
        0,
        minute_offset(
            origin,
            start_not_before or snapshot.snapshot_clock,
            round_up=True,
        ),
    )
    batch_by_id = {b.batch_id: b for b in batches}
    step_by_id = {s.step_id: s for s in snapshot.profile.routes}
    order_by_id = {o.order_id: o for o in snapshot.orders}
    operation_by_step = {(o.batch_id, o.step_id): o.operation_id for o in operations}
    resources = {r.resource_id: r for r in snapshot.resources if r.status == "AVAILABLE"}
    workers = {w.worker_id: w for w in snapshot.workers if w.status == "AVAILABLE"}
    resource_windows = {
        k: working_windows(snapshot, r.calendar, r.unavailable, overtime=allow_overtime)
        for k, r in resources.items()
    }
    worker_windows = {
        k: working_windows(
            snapshot, w.calendar, w.unavailable, overtime=allow_overtime and w.overtime_available
        )
        for k, w in workers.items()
    }
    resource_end: dict[str, int] = defaultdict(lambda: now)
    worker_end: dict[str, int] = defaultdict(lambda: now)
    last_product: dict[str, str] = {}
    operation_end: dict[str, int] = {}
    allocated: dict[str, int] = defaultdict(int)
    supply: dict[str, list[tuple[int, int]]] = {
        i.material_id: [(now, i.on_hand - i.reserved)] for i in snapshot.inventory
    }
    for receipt in snapshot.receipts:
        if receipt.status == "CONFIRMED":
            supply[receipt.material_id].append(
                (max(now, minute_offset(origin, receipt.eta, round_up=True)), receipt.quantity)
            )
    for values in supply.values():
        values.sort()
    locked_ids = {assignment.operation_id for assignment in locked}
    operations_by_id = {operation.operation_id: operation for operation in operations}
    pending = {o.operation_id: o for o in operations if o.operation_id not in locked_ids}
    result = list(locked)
    for assignment in locked:
        resource_end[assignment.resource_id] = max(
            resource_end[assignment.resource_id],
            minute_offset(origin, assignment.end_at, round_up=True),
        )
        worker_end[assignment.worker_id] = max(
            worker_end[assignment.worker_id],
            minute_offset(origin, assignment.end_at, round_up=True),
        )
        operation_end[assignment.operation_id] = minute_offset(
            origin, assignment.end_at, round_up=True
        )
    for assignment in sorted(locked, key=lambda a: a.end_at):
        operation = operations_by_id[assignment.operation_id]
        batch = batch_by_id[operation.batch_id]
        last_product[assignment.resource_id] = batch.product_id
        # Started batches already consumed or reserved their kit outside the free supply.
        if not step_by_id[operation.step_id].predecessors and batch.batch_id not in started_batches:
            for item in snapshot.profile.bom:
                if item.product_id == batch.product_id:
                    allocated[item.material_id] += item.quantity_per_unit * batch.quantity
    policy = snapshot.profile.policy
    while pending:
        best = None
        # A late placement of hard-deadline work still completes the hint; the solver
        # decides whether the deadline can be met.
        late = None
        for operation_id, operation in pending.items():
            batch = batch_by_id[operation.batch_id]
            step = step_by_id[operation.step_id]
            predecessors = [operation_by_step[batch.batch_id, p] for p in step.predecessors]
            if any(p not in operation_end for p in predecessors):
                continue
            earliest_start = max((operation_end[p] for p in predecessors), default=now)
            bom = [b for b in snapshot.profile.bom if b.product_id == batch.product_id]
            kit_possible = True
            if not predecessors:
                for item in bom:
                    needed = allocated[item.material_id] + item.quantity_per_unit * batch.quantity
                    available = 0
                    arrival = None
                    for at, quantity in supply[item.material_id]:
                        available += quantity
                        if available >= needed:
                            arrival = at
                            break
                    if arrival is None:
                        kit_possible = False
                        break
                    earliest_start = max(earliest_start, arrival)
            if not kit_possible:
                continue
            duration = duration_minutes(step, batch.quantity)
            order = order_by_id[batch.order_id]
            due = minute_offset(origin, order.due_at, round_up=False)
            for resource_id, resource in resources.items():
                if (
                    resource.resource_type != step.resource_type
                    or step.operation_code not in resource.operation_codes
                ):
                    continue
                previous = last_product.get(resource_id)
                change = (
                    policy.first_changeover_min
                    if previous is None
                    else (
                        policy.same_product_changeover_min
                        if previous == batch.product_id
                        else policy.different_product_changeover_min
                    )
                )
                for worker_id, worker in workers.items():
                    if step.skill not in worker.skills:
                        continue
                    begin = _first_common_slot(
                        resource_windows[resource_id],
                        worker_windows[worker_id],
                        max(
                            now,
                            resource_end[resource_id],
                            worker_end[worker_id],
                            earliest_start - change,
                        ),
                        duration + change,
                    )
                    if begin is None:
                        continue
                    end = begin + change + duration
                    candidate = (
                        (
                            begin,
                            not order.hard_deadline,
                            due,
                            -order.priority_weight,
                            operation_id,
                            resource_id,
                            worker_id,
                        ),
                        operation,
                        batch,
                        step,
                        resource_id,
                        worker_id,
                        begin,
                        change,
                        end,
                    )
                    if order.hard_deadline and end > due:
                        if late is None or candidate[0] < late[0]:
                            late = candidate
                        continue
                    key = (
                        begin,
                        not order.hard_deadline,
                        due,
                        -order.priority_weight,
                        operation_id,
                        resource_id,
                        worker_id,
                    )
                    if best is None or key < best[0]:
                        best = (
                            key,
                            operation,
                            batch,
                            step,
                            resource_id,
                            worker_id,
                            begin,
                            change,
                            end,
                        )
        best = best or late
        if best is None:
            return ()
        _, operation, batch, step, resource_id, worker_id, begin, change, end = best
        if not step.predecessors:
            for item in snapshot.profile.bom:
                if item.product_id == batch.product_id:
                    allocated[item.material_id] += item.quantity_per_unit * batch.quantity
        resource_end[resource_id] = worker_end[worker_id] = operation_end[
            operation.operation_id
        ] = end
        last_product[resource_id] = batch.product_id
        result.append(
            Assignment(
                operation_id=operation.operation_id,
                resource_id=resource_id,
                worker_id=worker_id,
                changeover_start=origin + timedelta(minutes=begin),
                start_at=origin + timedelta(minutes=begin + change),
                end_at=origin + timedelta(minutes=end),
            )
        )
        del pending[operation.operation_id]
    return tuple(result)
