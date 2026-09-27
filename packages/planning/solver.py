"""CP-SAT schedules from source facts; independent checking is mandatory before use."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import reduce
from typing import cast

from ortools.sat.python import cp_model

from packages.domain.models import (
    ActualExecution,
    Assignment,
    Candidate,
    CheckReport,
    Metric,
    NativeStatus,
    ScenarioFact,
    Snapshot,
    SolverPass,
    VersionBinding,
    batch_operations,
    duration_minutes,
    minute_offset,
    topological_route,
)
from packages.domain.objectives import EffectiveObjective
from packages.planning.dispatch import (
    continuation_dispatch,
    dispatch,
    recovery_dispatch,
    working_windows,
)
from packages.planning.disruption import recovery_operations
from packages.planning.execution_projection import first_changed_occupancy

OBJECTIVES = (
    "weighted_tardiness",
    "incremental_overtime_metric",
    "changed_operations",
    "total_start_shift",
    "makespan",
)
UNITS = ("minutes", "minutes", "operations", "minutes", "minutes")
SEED = 17
# Once a search has a checked incumbent, further polishing stops after this long without an
# improving solution. Remaining levels are then reported as unproven rather than spending the
# whole budget on tie-breakers such as makespan.
IMPROVEMENT_PATIENCE_SECONDS = 4.0


class PlanningInputError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class Task:
    start: cp_model.IntVar
    begin: cp_model.IntVar
    end: cp_model.IntVar
    change: cp_model.IntVar
    size: cp_model.IntVar
    resources: dict[str, cp_model.IntVar]
    workers: dict[str, cp_model.IntVar]
    product_id: str
    batch_id: str
    step_id: str
    duration: int
    actual: ActualExecution | None = None


def _reject_unsupported(snapshot: Snapshot, baseline: Candidate | None, budget: float) -> None:
    Snapshot.model_validate(snapshot.model_dump())
    if not math.isfinite(budget) or budget <= 0:
        raise PlanningInputError("INVALID_BUDGET", "A positive finite solve budget is required")
    if snapshot.actuals and snapshot.schema_version not in {"byof.snapshot/2", "byof.snapshot/3"}:
        raise PlanningInputError(
            "UNSUPPORTED_WIP", "Actual execution requires the versioned ledger"
        )
    if snapshot.active_plan_version is not None and baseline is None:
        raise PlanningInputError(
            "UNSUPPORTED_BASELINE", "The active source plan is required for freeze reconciliation"
        )
    if baseline is not None:
        Candidate.model_validate(baseline.model_dump())
        if (
            snapshot.active_plan_version is None
            or baseline.factory_id != snapshot.factory_id
            or baseline.content_hash != snapshot.active_plan_hash
            or not baseline.has_solution
        ):
            raise PlanningInputError(
                "BASELINE_MISMATCH", "Baseline does not match the active source plan"
            )
    if (
        any(o.status == "CANCELLED" for o in snapshot.orders)
        and snapshot.production_batches is None
    ):
        raise PlanningInputError(
            "UNSUPPORTED_ORDER_STATE", "Cancelled orders need explicit scope reconciliation"
        )
    if not snapshot.orders:
        raise PlanningInputError("EMPTY_SCOPE", "At least one confirmed order is required")
    if (
        not snapshot.source.complete
        or snapshot.source.freshness != "CURRENT"
        or snapshot.source.consistency == "UNVERIFIED"
    ):
        raise PlanningInputError(
            "SOURCE_INCOMPLETE", "Planning needs complete, current, consistent facts"
        )
    if snapshot.snapshot_clock >= snapshot.horizon.end_at:
        raise PlanningInputError("EXPIRED_HORIZON", "Business time has passed the planning horizon")
    _, operations = batch_operations(snapshot)
    steps = {s.step_id: s for s in snapshot.profile.routes}
    operation_steps = {o.operation_id: steps[o.step_id] for o in operations}
    resources = {r.resource_id: r for r in snapshot.resources}
    all_ids = {
        operation.operation_id
        for operation in batch_operations(snapshot, include_cancelled=True)[1]
    }
    if baseline and not {a.operation_id for a in baseline.assignments} <= all_ids:
        raise PlanningInputError(
            "BASELINE_SCOPE_MISMATCH", "Historical operations disappeared from scope"
        )
    for actual in snapshot.actuals:
        if actual.operation_id not in operation_steps:
            continue
        if actual.state == "COMPLETED":
            if (
                operation_steps[actual.operation_id].quality_gate
                and actual.quality_state != "PASSED"
            ):
                raise PlanningInputError(
                    "QUALITY_CONFIRMATION_REQUIRED", "Completed quality gate has not passed"
                )
            continue
        if (
            actual.remaining_minutes is None
            or actual.remaining_minutes <= 0
            or actual.remaining_setup_minutes is None
            or actual.remaining_confirmed_by is None
        ):
            raise PlanningInputError(
                "WIP_CONFIRMATION_REQUIRED",
                "Confirm remaining production and setup before rescheduling",
            )
        if (snapshot.snapshot_clock - snapshot.horizon.start_at) % timedelta(minutes=1):
            raise PlanningInputError(
                "UNSUPPORTED_TIME_PRECISION",
                "Continuous work requires a minute-aligned business clock",
            )
        if (
            actual.state in ("SETUP", "IN_PROGRESS")
            and resources[actual.resource_id].last_operation_id != actual.operation_id
        ):
            raise PlanningInputError(
                "WIP_STATE_CONFLICT", "Running work differs from the equipment setup state"
            )


def _overtime_minutes(snapshot: Snapshot, assignments: tuple[Assignment, ...]) -> int:
    workers = {w.worker_id: w for w in snapshot.workers}
    completed = {a.operation_id for a in snapshot.actuals if a.state == "COMPLETED"}
    duration = timedelta(0)
    for actual in snapshot.actuals:
        for segment in actual.segments:
            for window in workers[actual.worker_id].calendar:
                if window.kind == "OVERTIME":
                    duration += max(
                        timedelta(0),
                        min(segment.end_at, window.end_at) - max(segment.start_at, window.start_at),
                    )
    for assignment in assignments:
        if assignment.operation_id in completed:
            continue
        begin = max(
            snapshot.snapshot_clock,
            assignment.resume_changeover_start or assignment.changeover_start,
        )
        for window in workers[assignment.worker_id].calendar:
            if window.kind == "OVERTIME":
                duration += max(
                    timedelta(0),
                    min(assignment.end_at, window.end_at) - max(begin, window.start_at),
                )
    return -(-duration // timedelta(minutes=1))


def _continuation_hint(
    snapshot: Snapshot, baseline: Candidate | None, *, partial: bool = False
) -> tuple[Assignment, ...]:
    """Known positions for every operation, or with partial=True for those that have one."""
    if baseline is None:
        return ()
    prior = {a.operation_id: a for a in baseline.assignments}
    actuals = {a.operation_id: a for a in snapshot.actuals}
    resources = {r.resource_id: r for r in snapshot.resources}
    workers = {w.worker_id: w for w in snapshot.workers}
    batches, operations = batch_operations(snapshot)
    products = {b.batch_id: b.product_id for b in batches}
    steps = {s.step_id: s for s in snapshot.profile.routes}
    by_step = {(o.batch_id, o.step_id): o.operation_id for o in operations}
    ends: dict[str, datetime] = {}
    result = []
    for operation in sorted(operations, key=lambda o: len(_ancestors(steps, o.step_id))):
        actual = actuals.get(operation.operation_id)
        if actual is None:
            if operation.operation_id not in prior:
                if partial:
                    continue
                return ()
            result.append(prior[operation.operation_id])
            ends[operation.operation_id] = prior[operation.operation_id].end_at
            continue
        assert actual.changeover_start is not None
        if actual.state == "COMPLETED":
            assert actual.actual_start is not None and actual.actual_end is not None
            result.append(
                Assignment(
                    operation_id=actual.operation_id,
                    resource_id=actual.resource_id,
                    worker_id=actual.worker_id,
                    changeover_start=actual.changeover_start,
                    start_at=actual.actual_start,
                    end_at=actual.actual_end,
                )
            )
            ends[operation.operation_id] = actual.actual_end
            continue
        resource = resources[actual.resource_id]
        policy = snapshot.profile.policy
        setup = (
            actual.remaining_setup_minutes
            if resource.last_operation_id == actual.operation_id
            else policy.first_changeover_min
            if resource.last_product_id is None
            else policy.same_product_changeover_min
            if resource.last_product_id == products[actual.batch_id]
            else policy.different_product_changeover_min
        )
        assert setup is not None and actual.remaining_minutes is not None
        # Interrupted work resumes once its machine and person are free again.
        free = max(
            [snapshot.snapshot_clock]
            + [
                ends[by_step[operation.batch_id, p]]
                for p in steps[operation.step_id].predecessors
                if by_step.get((operation.batch_id, p)) in ends
            ]
        )
        for owner in (resource, workers.get(actual.worker_id)):
            free = _free_from(owner, free) if owner is not None else free
        resume = free + timedelta(minutes=setup)
        result.append(
            Assignment(
                operation_id=actual.operation_id,
                resource_id=actual.resource_id,
                worker_id=actual.worker_id,
                changeover_start=actual.changeover_start,
                start_at=actual.actual_start or resume,
                end_at=resume + timedelta(minutes=actual.remaining_minutes),
                resume_changeover_start=free,
                resume_at=resume,
            )
        )
        ends[operation.operation_id] = result[-1].end_at
    return tuple(result)


def _ancestors(steps: dict, step_id: str) -> set[str]:
    seen: set[str] = set()
    stack = list(steps[step_id].predecessors)
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(steps[current].predecessors)
    return seen


def _with_kept_predecessors(
    snapshot: Snapshot, kept: tuple[Assignment, ...]
) -> tuple[Assignment, ...]:
    """Keep an old position only when every predecessor is also kept or already finished."""
    steps = {s.step_id: s for s in snapshot.profile.routes}
    batches, operations = batch_operations(snapshot)
    by_step = {(o.batch_id, o.step_id): o.operation_id for o in operations}
    started = {a.operation_id for a in snapshot.actuals}
    finished = {a.operation_id for a in snapshot.actuals if a.state == "COMPLETED"}
    positions = {a.operation_id: a for a in kept}
    kept_ids = set(positions)
    for operation in sorted(operations, key=lambda o: len(_ancestors(steps, o.step_id))):
        identity = operation.operation_id
        if identity not in kept_ids or identity in started:
            continue
        # Work that has not started keeps its old time only after its predecessors finish.
        begin = positions[identity].start_at
        for step in steps[operation.step_id].predecessors:
            predecessor = by_step.get((operation.batch_id, step))
            if predecessor in finished:
                continue
            if predecessor not in kept_ids or positions[predecessor].end_at > begin:
                kept_ids.discard(identity)
                break
    return tuple(a for a in kept if a.operation_id in kept_ids)


def _free_from(owner, moment: datetime) -> datetime:
    """First moment at or after the given time outside the owner's unavailable windows."""
    moved = True
    while moved:
        moved = False
        for window in owner.unavailable:
            if window.start_at <= moment < window.end_at:
                moment, moved = window.end_at, True
    return moment


def _hint_passes(
    snapshot: Snapshot,
    hint: tuple[Assignment, ...],
    baseline: Candidate,
    allow_overtime: bool,
    new_actions_not_before: datetime | None,
) -> bool:
    """Whether a search hint is itself a physically valid plan under the current facts."""
    from packages.planning.checker import _check_physical_plan

    if not hint:
        return False
    report = _check_physical_plan(
        snapshot,
        hint,
        baseline=baseline,
        allow_overtime=allow_overtime,
        objective=None,
        effective_not_before=max(snapshot.snapshot_clock, snapshot.horizon.start_at),
        new_actions_not_before=new_actions_not_before,
    )
    return report.status == "PASS"


def _unavailable_assignment(snapshot: Snapshot, assignment: Assignment) -> bool:
    """Whether an unstarted planned operation now sits on a machine or person that is unavailable."""
    begin, end = assignment.changeover_start, assignment.end_at
    for owner in (
        next((r for r in snapshot.resources if r.resource_id == assignment.resource_id), None),
        next((w for w in snapshot.workers if w.worker_id == assignment.worker_id), None),
    ):
        if owner is None or owner.status != "AVAILABLE":
            return True
        if any(window.start_at < end and begin < window.end_at for window in owner.unavailable):
            return True
    return False


def _blocked_intervals(model, snapshot, calendar, unavailable, overtime, prefix):
    horizon = minute_offset(snapshot.horizon.start_at, snapshot.horizon.end_at, round_up=False)
    result = []
    cursor = 0
    for start, end in working_windows(snapshot, calendar, unavailable, overtime=overtime):
        if cursor < start:
            result.append(model.new_fixed_size_interval_var(cursor, start - cursor, prefix))
        cursor = end
    if cursor < horizon:
        result.append(model.new_fixed_size_interval_var(cursor, horizon - cursor, prefix))
    return result


def _build(
    snapshot: Snapshot,
    allow_overtime: bool,
    hint: tuple[Assignment, ...],
    baseline: Candidate | None = None,
    new_actions_not_before: datetime | None = None,
):
    model = cp_model.CpModel()
    origin = snapshot.horizon.start_at
    horizon = minute_offset(origin, snapshot.horizon.end_at, round_up=False)
    now = max(0, minute_offset(origin, snapshot.snapshot_clock, round_up=True))
    if now > horizon:
        model.add_bool_or([])
        now = horizon
    policy = snapshot.profile.policy
    max_change = max(
        policy.first_changeover_min,
        policy.same_product_changeover_min,
        policy.different_product_changeover_min,
        *(a.remaining_setup_minutes or 0 for a in snapshot.actuals),
    )
    batches, operations = batch_operations(snapshot)
    batch_by_id = {b.batch_id: b for b in batches}
    step_by_id = {s.step_id: s for s in snapshot.profile.routes}
    operation_by_step = {(o.batch_id, o.step_id): o.operation_id for o in operations}
    resource_intervals: dict[str, list] = defaultdict(list)
    worker_intervals: dict[str, list] = defaultdict(list)
    resource_nodes: dict[str, list[str]] = defaultdict(list)
    hint_by_id = {a.operation_id: a for a in hint}
    actuals = {a.operation_id: a for a in snapshot.actuals}
    baseline_by_id = {a.operation_id: a for a in baseline.assignments} if baseline else {}
    freeze_end = snapshot.snapshot_clock + timedelta(minutes=policy.freeze_window_min)
    recovery = recovery_operations(snapshot, baseline) if baseline is not None else set()
    tasks: dict[str, Task] = {}
    overtime_terms = []
    overtime_hint = 0
    for operation in operations:
        identifier = operation.operation_id
        batch = batch_by_id[operation.batch_id]
        step = step_by_id[operation.step_id]
        actual = actuals.get(identifier)
        if actual is not None and actual.state == "COMPLETED":
            assert (
                actual.actual_start is not None
                and actual.actual_end is not None
                and actual.changeover_start is not None
            )
            hbegin = minute_offset(origin, actual.changeover_start, round_up=True)
            hstart = minute_offset(origin, actual.actual_start, round_up=True)
            hend = minute_offset(origin, actual.actual_end, round_up=True)
            tasks[identifier] = Task(
                model.new_constant(hstart),
                model.new_constant(hbegin),
                model.new_constant(hend),
                model.new_constant(hstart - hbegin),
                model.new_constant(hend - hbegin),
                {actual.resource_id: model.new_constant(1)},
                {actual.worker_id: model.new_constant(1)},
                batch.product_id,
                batch.batch_id,
                step.step_id,
                0,
                actual,
            )
            continue
        duration = actual.remaining_minutes if actual else duration_minutes(step, batch.quantity)
        assert duration is not None
        begin = model.new_int_var(now, horizon, f"{identifier}:changeover_start")
        start = model.new_int_var(now, horizon, f"{identifier}:start")
        end = model.new_int_var(now, horizon, f"{identifier}:end")
        change = model.new_int_var(0, max_change, f"{identifier}:changeover")
        size = model.new_int_var(duration, duration + max_change, f"{identifier}:occupancy")
        model.add(start == begin + change)
        model.add(end == start + duration)
        model.add(size == duration + change)
        resource_choices = {}
        worker_choices = {}
        suggested = hint_by_id.get(identifier)
        for resource in snapshot.resources:
            if (
                resource.status != "AVAILABLE"
                or resource.resource_type != step.resource_type
                or step.operation_code not in resource.operation_codes
                or (actual is not None and actual.resource_id != resource.resource_id)
            ):
                continue
            selected = model.new_bool_var(f"{identifier}:resource:{resource.resource_id}")
            resource_choices[resource.resource_id] = selected
            resource_nodes[resource.resource_id].append(identifier)
            resource_intervals[resource.resource_id].append(
                model.new_optional_interval_var(
                    begin, size, end, selected, f"{identifier}@{resource.resource_id}"
                )
            )
            if suggested:
                model.add_hint(selected, int(suggested.resource_id == resource.resource_id))
        for worker in snapshot.workers:
            if (
                worker.status != "AVAILABLE"
                or step.skill not in worker.skills
                or (actual is not None and actual.worker_id != worker.worker_id)
            ):
                continue
            selected = model.new_bool_var(f"{identifier}:worker:{worker.worker_id}")
            worker_choices[worker.worker_id] = selected
            worker_intervals[worker.worker_id].append(
                model.new_optional_interval_var(
                    begin, size, end, selected, f"{identifier}@{worker.worker_id}"
                )
            )
            if suggested:
                model.add_hint(selected, int(suggested.worker_id == worker.worker_id))
            if allow_overtime and worker.overtime_available:
                for calendar in worker.calendar:
                    if calendar.kind != "OVERTIME":
                        continue
                    left = max(0, minute_offset(origin, calendar.start_at, round_up=True))
                    right = min(horizon, minute_offset(origin, calendar.end_at, round_up=False))
                    if left >= right:
                        continue
                    clipped_start = model.new_int_var(0, horizon, "overtime:start")
                    clipped_end = model.new_int_var(0, horizon, "overtime:end")
                    overlap = model.new_int_var(0, duration + max_change, "overtime:overlap")
                    paid = model.new_int_var(0, duration + max_change, "overtime:worker_minutes")
                    model.add_max_equality(clipped_start, [begin, left])
                    model.add_min_equality(clipped_end, [end, right])
                    model.add_max_equality(overlap, [0, clipped_end - clipped_start])
                    model.add(paid == overlap).only_enforce_if(selected)
                    model.add(paid == 0).only_enforce_if(selected.Not())
                    overtime_terms.append(paid)
                    if suggested:
                        hbegin = minute_offset(
                            origin,
                            suggested.resume_changeover_start or suggested.changeover_start,
                            round_up=True,
                        )
                        hend = minute_offset(origin, suggested.end_at, round_up=True)
                        hstart = max(hbegin, left)
                        hfinish = min(hend, right)
                        hoverlap = max(0, hfinish - hstart)
                        hpaid = hoverlap if suggested.worker_id == worker.worker_id else 0
                        for variable, value in [
                            (clipped_start, hstart),
                            (clipped_end, hfinish),
                            (overlap, hoverlap),
                            (paid, hpaid),
                        ]:
                            model.add_hint(variable, value)
                        overtime_hint += hpaid
        model.add_exactly_one(resource_choices.values())
        model.add_exactly_one(worker_choices.values())
        tasks[identifier] = Task(
            start,
            begin,
            end,
            change,
            size,
            resource_choices,
            worker_choices,
            batch.product_id,
            batch.batch_id,
            step.step_id,
            duration,
            actual,
        )
        if actual is not None and actual.state in ("SETUP", "IN_PROGRESS"):
            model.add(begin == now)
        old = baseline_by_id.get(identifier)
        if new_actions_not_before is not None:
            boundary = minute_offset(origin, new_actions_not_before, round_up=True)
            if actual is None:
                if (
                    old is not None
                    and identifier not in recovery
                    and snapshot.snapshot_clock <= old.changeover_start < new_actions_not_before
                ):
                    # Work already authorized inside the review interval keeps its exact dispatch.
                    model.add(begin == minute_offset(origin, old.changeover_start, round_up=True))
                    model.add(start == minute_offset(origin, old.start_at, round_up=True))
                    model.add(end == minute_offset(origin, old.end_at, round_up=True))
                    model.add(resource_choices.get(old.resource_id, 0) == 1)
                    model.add(worker_choices.get(old.worker_id, 0) == 1)
                else:
                    model.add(begin >= boundary)
            elif actual.state not in {"SETUP", "IN_PROGRESS"}:
                model.add(begin >= boundary)
        if (
            actual is None
            and identifier not in recovery
            and old is not None
            and snapshot.snapshot_clock <= old.start_at < freeze_end
        ):
            model.add(start == minute_offset(origin, old.start_at, round_up=True))
            model.add(resource_choices.get(old.resource_id, 0) == 1)
            model.add(worker_choices.get(old.worker_id, 0) == 1)
        if suggested:
            hbegin = minute_offset(
                origin,
                suggested.resume_changeover_start or suggested.changeover_start,
                round_up=True,
            )
            hstart = minute_offset(origin, suggested.resume_at or suggested.start_at, round_up=True)
            hend = minute_offset(origin, suggested.end_at, round_up=True)
            for variable, value in [
                (begin, hbegin),
                (start, hstart),
                (end, hend),
                (change, hstart - hbegin),
                (size, hend - hbegin),
            ]:
                model.add_hint(variable, value)
    ancestors: dict[str, set[str]] = {}
    for product in snapshot.profile.products:
        route = topological_route(
            tuple(s for s in snapshot.profile.routes if s.product_id == product.product_id)
        )
        for step in route:
            ancestors[step.step_id] = set(step.predecessors)
            for predecessor in step.predecessors:
                ancestors[step.step_id].update(ancestors[predecessor])
    for identifier, task in tasks.items():
        if task.actual is not None and task.actual.state == "COMPLETED":
            continue
        step = step_by_id[task.step_id]
        for predecessor in step.predecessors:
            model.add(task.start >= tasks[operation_by_step[task.batch_id, predecessor]].end)
    arc_count = 0
    for resource in snapshot.resources:
        ids = resource_nodes[resource.resource_id]
        if not ids:
            continue
        occupied = resource_intervals[resource.resource_id]
        occupied += _blocked_intervals(
            model,
            snapshot,
            resource.calendar,
            resource.unavailable,
            allow_overtime,
            "resource:unavailable",
        )
        model.add_no_overlap(occupied)
        sorted_hint = sorted(
            (a for a in hint if a.resource_id == resource.resource_id and a.operation_id in ids),
            key=lambda a: a.resume_changeover_start or a.changeover_start,
        )
        hint_nodes = [0] + [ids.index(a.operation_id) + 1 for a in sorted_hint] + [0]
        hint_edges = set(zip(hint_nodes, hint_nodes[1:]))
        empty = model.new_bool_var(f"{resource.resource_id}:empty")
        model.add(sum(tasks[i].resources[resource.resource_id] for i in ids) == 0).only_enforce_if(
            empty
        )
        model.add(sum(tasks[i].resources[resource.resource_id] for i in ids) >= 1).only_enforce_if(
            empty.Not()
        )
        arcs: list = [(0, 0, empty)]
        if hint:
            model.add_hint(empty, int(not sorted_hint))
        incoming: dict[int, list] = defaultdict(list)
        for index, identifier in enumerate(ids, 1):
            task = tasks[identifier]
            arcs.append((index, index, task.resources[resource.resource_id].Not()))
            first = model.new_bool_var(f"{resource.resource_id}:first:{index}")
            last = model.new_bool_var(f"{resource.resource_id}:last:{index}")
            arcs.extend([(0, index, first), (index, 0, last)])
            first_change = (
                task.actual.remaining_setup_minutes
                if task.actual is not None and resource.last_operation_id == identifier
                else policy.first_changeover_min
                if resource.last_product_id is None
                else policy.same_product_changeover_min
                if resource.last_product_id == task.product_id
                else policy.different_product_changeover_min
            )
            assert first_change is not None
            incoming[index].append(first_change * first)
            if hint:
                model.add_hint(first, int((0, index) in hint_edges))
                model.add_hint(last, int((index, 0) in hint_edges))
            for following, destination in enumerate(ids, 1):
                successor = tasks[destination]
                if following == index or (
                    task.batch_id == successor.batch_id
                    and successor.step_id in ancestors[task.step_id]
                ):
                    continue
                edge = model.new_bool_var(f"{resource.resource_id}:{index}>{following}")
                arcs.append((index, following, edge))
                model.add(successor.begin >= task.end).only_enforce_if(edge)
                change_minutes = (
                    policy.same_product_changeover_min
                    if task.product_id == successor.product_id
                    else policy.different_product_changeover_min
                )
                incoming[following].append(change_minutes * edge)
                if hint:
                    model.add_hint(edge, int((index, following) in hint_edges))
                arc_count += 1
        model.add_circuit(arcs)
        for index, identifier in enumerate(ids, 1):
            model.add(tasks[identifier].change == sum(incoming[index])).only_enforce_if(
                tasks[identifier].resources[resource.resource_id]
            )
    for worker in snapshot.workers:
        if worker.worker_id in worker_intervals:
            intervals = worker_intervals[worker.worker_id]
            intervals += _blocked_intervals(
                model,
                snapshot,
                worker.calendar,
                worker.unavailable,
                allow_overtime and worker.overtime_available,
                "worker:unavailable",
            )
            model.add_no_overlap(intervals)
    roots = {
        b.batch_id: next(
            o.operation_id
            for o in operations
            if o.batch_id == b.batch_id and not step_by_id[o.step_id].predecessors
        )
        for b in batches
    }
    started_batches = {a.batch_id for a in snapshot.actuals if a.actual_start is not None}
    supply_signatures = set()
    supply_constraints = 0
    for inventory in snapshot.inventory:
        requirements = []
        for batch in batches:
            if batch.batch_id in started_batches:
                continue
            amount = sum(
                item.quantity_per_unit * batch.quantity
                for item in snapshot.profile.bom
                if item.product_id == batch.product_id and item.material_id == inventory.material_id
            )
            if amount:
                requirements.append((roots[batch.batch_id], amount))
        opening = inventory.on_hand - inventory.reserved
        if opening >= sum(amount for _, amount in requirements):
            continue
        arrivals: dict[int, int] = defaultdict(int)
        arrivals[0] = opening
        for receipt in snapshot.receipts:
            if receipt.material_id == inventory.material_id and receipt.status == "CONFIRMED":
                arrivals[max(0, minute_offset(origin, receipt.eta, round_up=True))] += (
                    receipt.quantity
                )
        divisor = (
            reduce(math.gcd, [amount for _, amount in requirements] + list(arrivals.values())) or 1
        )
        signature = (
            tuple((key, amount // divisor) for key, amount in requirements),
            tuple((at, amount // divisor) for at, amount in sorted(arrivals.items())),
        )
        # Proportional BOM supplies impose the same reservoir inequality after exact GCD scaling.
        if signature in supply_signatures:
            continue
        supply_signatures.add(signature)
        times = [at for at, _ in sorted(arrivals.items())] + [
            tasks[key].start for key, _ in requirements
        ]
        levels = [amount // divisor for _, amount in sorted(arrivals.items())] + [
            -amount // divisor for _, amount in requirements
        ]
        model.add_reservoir_constraint(times, levels, 0, sum(max(0, value) for value in levels))
        supply_constraints += 1
    # Identical batches in one unstarted order may be relabelled by their first start.
    for order in () if baseline is not None or snapshot.actuals else snapshot.orders:
        order_batches = [b for b in batches if b.order_id == order.order_id]
        for first_batch, next_batch in zip(order_batches, order_batches[1:]):
            model.add(
                tasks[roots[first_batch.batch_id]].start <= tasks[roots[next_batch.batch_id]].start
            )
    tardiness_terms: list = []
    tardiness_upper = 0
    tardiness_hint = 0
    customer_batches = (
        {batch.batch_id for batch in snapshot.production_batches if batch.purpose == "CUSTOMER"}
        if snapshot.production_batches is not None
        else set(batch_by_id)
    )
    for production_batch in snapshot.production_batches or ():
        if production_batch.purpose == "CUSTOMER" and production_batch.delivery_due_at is not None:
            for operation in operations:
                if operation.batch_id == production_batch.batch_id:
                    model.add(
                        tasks[operation.operation_id].end
                        <= minute_offset(origin, production_batch.delivery_due_at, round_up=False)
                    )
    for order in snapshot.orders:
        ids = [
            o.operation_id
            for o in operations
            if batch_by_id[o.batch_id].order_id == order.order_id and o.batch_id in customer_batches
        ]
        if not ids:
            continue
        if all(i in actuals and actuals[i].state == "COMPLETED" for i in ids):
            ends = [actuals[i].actual_end for i in ids]
            completion = max(end for end in ends if end is not None)
            delay = max(0, minute_offset(order.due_at, completion, round_up=True))
            if order.hard_deadline:
                model.add(completion <= order.due_at)
            tardiness_terms.append(order.priority_weight * delay)
            tardiness_upper += order.priority_weight * delay
            tardiness_hint += order.priority_weight * delay
            continue
        finish = model.new_int_var(now, horizon, f"{order.order_id}:completion")
        model.add_max_equality(finish, [tasks[i].end for i in ids])
        due = minute_offset(origin, order.due_at, round_up=False)
        if order.hard_deadline:
            model.add(finish <= due)
        late = model.new_int_var(0, max(0, horizon - due), f"{order.order_id}:tardiness")
        model.add_max_equality(late, [0, finish - due])
        tardiness_terms.append(order.priority_weight * late)
        tardiness_upper += order.priority_weight * max(0, horizon - due)
        if hint:
            hfinish = max(minute_offset(origin, hint_by_id[i].end_at, round_up=True) for i in ids)
            model.add_hint(finish, hfinish)
            model.add_hint(late, max(0, hfinish - due))
            tardiness_hint += order.priority_weight * max(0, hfinish - due)
    changed_terms = []
    shift_terms: list = []
    shift_upper = 0
    changed_hint = shift_hint = 0

    def differs(variable, timestamp, name, suggested):
        if (timestamp - origin) % timedelta(minutes=1):
            return model.new_constant(1)
        offset = minute_offset(origin, timestamp, round_up=True)
        different = model.new_bool_var(name)
        model.add(variable != offset).only_enforce_if(different)
        model.add(variable == offset).only_enforce_if(different.Not())
        if suggested is not None:
            model.add_hint(different, int(suggested != timestamp))
        return different

    for identifier, before in baseline_by_id.items():
        if identifier not in tasks:
            continue
        task = tasks[identifier]
        actual = task.actual
        if actual is not None and actual.state == "COMPLETED":
            continue
        suggested = hint_by_id.get(identifier)
        changes = [
            differs(
                task.end,
                before.end_at,
                identifier + ":end_changed",
                suggested.end_at if suggested else None,
            ),
            task.resources[before.resource_id].Not()
            if before.resource_id in task.resources
            else model.new_constant(1),
            task.workers[before.worker_id].Not()
            if before.worker_id in task.workers
            else model.new_constant(1),
        ]
        if actual is not None and actual.actual_start is not None:
            changes.append(model.new_constant(int(actual.actual_start != before.start_at)))
            fixed_shift = abs(minute_offset(before.start_at, actual.actual_start, round_up=False))
            shift_terms.append(fixed_shift)
            shift_upper += fixed_shift
        else:
            changes.append(
                differs(
                    task.start,
                    before.start_at,
                    identifier + ":start_changed",
                    suggested.start_at if suggested else None,
                )
            )
            old_start = minute_offset(origin, before.start_at, round_up=True)
            bound = max(abs(now - old_start), abs(horizon - old_start))
            deviation = model.new_int_var(0, bound, identifier + ":start_shift")
            model.add_abs_equality(deviation, task.start - old_start)
            shift_terms.append(deviation)
            shift_upper += bound
            if hint:
                model.add_hint(
                    deviation,
                    abs(
                        minute_offset(
                            before.start_at, hint_by_id[identifier].start_at, round_up=False
                        )
                    ),
                )
        if actual is not None:
            changes.append(
                model.new_constant(int(actual.changeover_start != before.changeover_start))
            )
        else:
            changes.append(
                differs(
                    task.begin,
                    before.changeover_start,
                    identifier + ":setup_changed",
                    suggested.changeover_start if suggested else None,
                )
            )
        changed = model.new_bool_var(identifier + ":changed")
        model.add_max_equality(changed, changes)
        changed_terms.append(changed)
        if hint:
            after = hint_by_id[identifier]
            different = (
                before.start_at,
                before.end_at,
                before.changeover_start,
                before.resource_id,
                before.worker_id,
            ) != (
                after.start_at,
                after.end_at,
                after.changeover_start,
                after.resource_id,
                after.worker_id,
            )
            changed_hint += int(different)
            shift_hint += abs(minute_offset(before.start_at, after.start_at, round_up=False))
            model.add_hint(changed, int(different))
    overtime_constant = _overtime_minutes(snapshot, ()) - (
        _overtime_minutes(snapshot, baseline.assignments) if baseline else 0
    )
    overtime_upper = overtime_constant + (
        sum(
            t.duration + max_change
            for t in tasks.values()
            if t.actual is None or t.actual.state != "COMPLETED"
        )
        if allow_overtime
        else 0
    )
    upper_bounds = (tardiness_upper, overtime_upper, len(changed_terms), shift_upper, horizon)
    lower_bounds = (0, overtime_constant, 0, 0, 0)
    objective_vars = [
        model.new_int_var(lower, upper, name)
        for lower, upper, name in zip(lower_bounds, upper_bounds, OBJECTIVES)
    ]
    model.add(objective_vars[0] == sum(tardiness_terms))
    model.add(objective_vars[1] == overtime_constant + sum(overtime_terms))
    model.add(objective_vars[2] == sum(changed_terms))
    model.add(objective_vars[3] == sum(shift_terms))
    if tasks:
        model.add_max_equality(objective_vars[4], [t.end for t in tasks.values()])
    else:
        model.add(objective_vars[4] == 0)
    if hint:
        values = (
            tardiness_hint,
            overtime_constant + overtime_hint,
            changed_hint,
            shift_hint,
            max(minute_offset(origin, a.end_at, round_up=True) for a in hint),
        )
        for variable, value in zip(objective_vars, values):
            model.add_hint(variable, value)
        hinted = set(model.proto.solution_hint.vars)
        for index, variable in enumerate(model.proto.variables):
            domain = tuple(variable.domain)
            if index not in hinted and domain[0] == domain[-1]:
                model.add_hint(model.get_int_var_from_proto_index(index), domain[0])
    return (
        model,
        tasks,
        objective_vars,
        {
            "sequence_arcs": arc_count,
            "distinct_supply_constraints": supply_constraints,
            "objective_upper_bounds": upper_bounds,
            "completed_operations": sum(a.state == "COMPLETED" for a in snapshot.actuals),
            "continued_operations": sum(a.state != "COMPLETED" for a in snapshot.actuals),
            "baseline_hash": baseline.content_hash if baseline else None,
        },
    )


@dataclass
class LexicographicResult:
    incumbent: cp_model.CpSolver | None
    last_solver: cp_model.CpSolver
    passes: tuple[SolverPass, ...]
    proven: int
    lower_bounds: tuple[int | None, ...]
    constant_levels: tuple[str, ...]
    stopped_for_budget: bool


def _configured_solver(seconds: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.0, seconds)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = SEED
    solver.parameters.absolute_gap_limit = 0
    solver.parameters.relative_gap_limit = 0
    solver.parameters.cp_model_presolve = False
    solver.parameters.search_branching = cp_model.HINT_SEARCH
    return solver


class _ImprovementClock(cp_model.CpSolverSolutionCallback):
    def __init__(self) -> None:
        super().__init__()
        self.last_improvement: float | None = None

    def on_solution_callback(self) -> None:
        self.last_improvement = time.perf_counter()


def _solve_with_patience(solver: cp_model.CpSolver, model: cp_model.CpModel):
    """Solve one level and stop it once improvements have stalled after a first solution."""
    clock = _ImprovementClock()
    done = threading.Event()

    def watch() -> None:
        while not done.wait(0.1):
            last = clock.last_improvement
            if last is not None and time.perf_counter() - last >= IMPROVEMENT_PATIENCE_SECONDS:
                solver.stop_search()
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        return solver.solve(model, clock)
    finally:
        done.set()
        watcher.join()


def _search_lexicographic(
    model: cp_model.CpModel,
    objectives: list[cp_model.IntVar],
    *,
    deadline: float,
    names: tuple[str, ...] = OBJECTIVES,
) -> LexicographicResult:
    incumbent = None
    passes = []
    lower_bounds: list[int | None] = [None] * len(objectives)
    constant_levels = []
    proven = 0
    stopped_for_budget = False
    for index, objective in enumerate(objectives):
        domain = tuple(objective.proto.domain)
        if incumbent is not None and domain[0] == domain[-1]:
            lower_bounds[index] = int(domain[0])
            constant_levels.append(names[index])
            proven += 1
            continue
        remaining = deadline - time.perf_counter()
        if remaining <= 0 and incumbent is not None:
            stopped_for_budget = True
            break
        model.minimize(objective)
        solver = _configured_solver(remaining)
        status = _solve_with_patience(solver, model)
        native = cast(NativeStatus, solver.status_name(status))
        has_solution = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        value = int(solver.value(objective)) if has_solution else None
        passes.append(
            SolverPass(
                objective_name=names[index],
                native_status=native,
                has_solution=has_solution,
                objective_value=value,
                best_bound=solver.best_objective_bound if has_solution else None,
                wall_time_seconds=solver.wall_time,
            )
        )
        if has_solution:
            incumbent = solver
            if status == cp_model.OPTIMAL:
                # Exact integer values certify this level; the native float bound is kept separately.
                lower_bounds[index] = value
            elif abs(solver.best_objective_bound) < 2**53:
                lower_bounds[index] = max(
                    int(domain[0]),
                    math.floor(math.nextafter(solver.best_objective_bound, -math.inf)),
                )
        if status != cp_model.OPTIMAL:
            if status == cp_model.INFEASIBLE and incumbent is not None:
                raise PlanningInputError(
                    "LEX_SEARCH_INCONSISTENT",
                    "A later objective contradicted the retained feasible assignment",
                )
            stopped_for_budget = status in (cp_model.UNKNOWN, cp_model.FEASIBLE)
            break
        proven += 1
        model.add(objective == value)
        if index + 1 < len(objectives):
            model.clear_hints()
            for variable_index, solution_value in enumerate(solver.response_proto.solution):
                variable = model.get_int_var_from_proto_index(variable_index)
                model.add_hint(variable, solution_value)
    return LexicographicResult(
        incumbent=incumbent,
        last_solver=solver,
        passes=tuple(passes),
        proven=proven,
        lower_bounds=tuple(lower_bounds),
        constant_levels=tuple(constant_levels),
        stopped_for_budget=stopped_for_budget,
    )


def _preset_candidate(
    snapshot: Snapshot,
    objective: EffectiveObjective | None,
    objective_version: str,
    started: float,
):
    """Use a verified opening preset for untouched facts; any mismatch falls back to solving."""
    from packages.planning.checker import calculate_metrics, check_candidate
    from packages.planning.presets import opening_preset

    found = opening_preset(snapshot)
    if found is None:
        return None
    entry, assignments = found
    try:
        metrics = calculate_metrics(snapshot, assignments)
    except ValueError:
        return None
    origin = snapshot.horizon.start_at
    effective = max(snapshot.snapshot_clock, origin)
    accept_before = min(
        snapshot.horizon.end_at,
        (
            first_changed_occupancy(snapshot, assignments, None)
            if snapshot.profile.policy.progress_revalidation_enabled
            else min(a.changeover_start for a in assignments)
        )
        + timedelta(minutes=1),
    )
    snapshot_hash = str(snapshot.content_hash)
    candidate = Candidate(
        schema_version="byof.candidate/2",
        candidate_id=str(uuid.uuid4()),
        factory_id=snapshot.factory_id,
        version=1,
        binding=VersionBinding(
            snapshot_hash=snapshot_hash,
            planning_revision=snapshot.planning_revision,
            scope_version=snapshot.scope_version,
            profile_version=snapshot.profile.version,
            policy_version=snapshot.profile.policy.policy_version,
            objective_version=objective_version,
            baseline_plan_version=snapshot.active_plan_version,
        ),
        native_status="FEASIBLE",
        has_solution=True,
        termination_reason="COMPLETED",
        objective=tuple(
            metric.model_copy(update={"lower_bound": None, "unknown_reason": None})
            for metric in metrics
        ),
        assignments=assignments,
        checker=CheckReport(
            checker_version="not-run", snapshot_hash=snapshot_hash, status="NOT_RUN"
        ),
        effective_not_before=effective,
        accept_before=accept_before,
    )
    report = check_candidate(snapshot, candidate, baseline=None, objective=objective)
    if report.status != "PASS":
        return None
    data = candidate.model_dump(exclude={"content_hash"})
    data["checker"] = report
    evidence = {
        "solver": "opening-preset",
        "preset_scenario": entry.get("scenario"),
        "preset_generated_with": entry.get("generated_with"),
        "hint_operations": len(assignments),
        "native_status": "FEASIBLE",
        "snapshot_hash": snapshot_hash,
        "objective_version": objective_version,
        "total_elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    return Candidate.model_validate(data), evidence


def solve_with_evidence(
    snapshot: Snapshot,
    *,
    time_limit: float = 30,
    allow_overtime: bool = False,
    baseline: Candidate | None = None,
    objective: EffectiveObjective | None = None,
    new_actions_not_before: datetime | None = None,
):
    started = time.perf_counter()
    _reject_unsupported(snapshot, baseline, time_limit)
    if new_actions_not_before is not None and (
        new_actions_not_before.tzinfo is None
        or not snapshot.snapshot_clock <= new_actions_not_before < snapshot.horizon.end_at
        or new_actions_not_before.second
        or new_actions_not_before.microsecond
    ):
        raise PlanningInputError(
            "INVALID_ACTION_TIME",
            "New actions need a future whole business minute within the horizon",
        )
    if objective is not None:
        try:
            objective.validate_for(snapshot)
        except ValueError as exc:
            raise PlanningInputError(
                "OBJECTIVE_MISMATCH", "Objective contract does not match the factory configuration"
            ) from exc
    names = objective.order if objective is not None else OBJECTIVES
    objective_version = objective.objective_version if objective is not None else "delivery-v1"
    if (
        baseline is None
        and not allow_overtime
        and new_actions_not_before is None
        and (objective is None or (tuple(names) == OBJECTIVES and not objective.bounds))
    ):
        preset = _preset_candidate(snapshot, objective, objective_version, started)
        if preset is not None:
            return preset
    if baseline is not None and not snapshot.actuals:
        hint = recovery_dispatch(
            snapshot,
            baseline,
            new_actions_not_before=new_actions_not_before or snapshot.snapshot_clock,
            allow_overtime=allow_overtime,
        )
        if not hint:
            hint = _continuation_hint(snapshot, baseline)
    elif (
        baseline is not None
        or snapshot.actuals
        or any(order.status != "CONFIRMED" for order in snapshot.orders)
    ):
        hint = _continuation_hint(snapshot, baseline)
        in_progress = {actual.operation_id for actual in snapshot.actuals}
        # Unstarted work whose old time has passed (a missed dispatch) or that now sits
        # on an unavailable machine or person needs a new position.
        blocked = {
            a.operation_id
            for a in hint
            if a.operation_id not in in_progress
            and (
                a.changeover_start < snapshot.snapshot_clock or _unavailable_assignment(snapshot, a)
            )
        }
        if baseline is not None and (
            not hint
            or blocked
            or not _hint_passes(snapshot, hint, baseline, allow_overtime, new_actions_not_before)
        ):
            # New demand has no old position, and a disruption (equipment, people, later
            # material) makes old positions invalid; keep started and frozen work, and place
            # the rest heuristically instead of searching without a usable hint.
            freeze_end = snapshot.snapshot_clock + timedelta(
                minutes=snapshot.profile.policy.freeze_window_min
            )
            partial = _continuation_hint(snapshot, baseline, partial=True)

            def replaced(keep_frozen: bool) -> tuple[Assignment, ...]:
                kept = tuple(
                    assignment
                    for assignment in partial
                    if assignment.operation_id not in blocked
                    and (
                        assignment.operation_id in in_progress
                        or (keep_frozen and assignment.changeover_start < freeze_end)
                    )
                )
                return continuation_dispatch(
                    snapshot,
                    _with_kept_predecessors(snapshot, kept),
                    new_actions_not_before=new_actions_not_before or snapshot.snapshot_clock,
                    allow_overtime=allow_overtime,
                )

            hint = replaced(True)
            if not _hint_passes(snapshot, hint, baseline, allow_overtime, new_actions_not_before):
                # Frozen work that the change itself broke is re-placed as well.
                hint = replaced(False)
    else:
        hint = dispatch(
            snapshot,
            allow_overtime=allow_overtime,
            new_actions_not_before=new_actions_not_before,
        )
    model, tasks, objective_vars, evidence = _build(
        snapshot, allow_overtime, hint, baseline, new_actions_not_before
    )
    if objective is not None:
        by_name = dict(zip(OBJECTIVES, objective_vars))
        for bounded_name, bound in objective.bounds.items():
            variable = by_name[bounded_name]
            domain = tuple(variable.proto.domain)
            lower, upper = domain[0], domain[-1]
            # Bounds outside the finite model domain are exact constants, even beyond int64.
            if bound < lower:
                model.add_bool_or([])
            elif bound < upper:
                model.add(variable <= bound)
        objective_vars = [by_name[name] for name in names]
        upper_bounds = dict(zip(OBJECTIVES, evidence["objective_upper_bounds"]))
        evidence["objective_upper_bounds"] = tuple(upper_bounds[name] for name in names)
        evidence["objective_bounds"] = objective.bounds
    built = time.perf_counter()
    search = _search_lexicographic(
        model, objective_vars, deadline=started + time_limit, names=names
    )
    solver = search.incumbent or search.last_solver
    last_search_status = search.passes[-1].native_status
    native = next(
        (p.native_status for p in reversed(search.passes) if p.has_solution), last_search_status
    )
    has_solution = search.incumbent is not None
    origin = snapshot.horizon.start_at
    assignments = []
    if has_solution:
        for identifier, task in tasks.items():
            actual = task.actual
            completed = actual is not None and actual.state == "COMPLETED"
            future_begin = origin + timedelta(minutes=solver.value(task.begin))
            future_start = origin + timedelta(minutes=solver.value(task.start))
            future_end = origin + timedelta(minutes=solver.value(task.end))
            assignments.append(
                Assignment(
                    operation_id=identifier,
                    resource_id=next(
                        key
                        for key, selected in task.resources.items()
                        if solver.boolean_value(selected)
                    ),
                    worker_id=next(
                        key
                        for key, selected in task.workers.items()
                        if solver.boolean_value(selected)
                    ),
                    changeover_start=actual.changeover_start if actual else future_begin,
                    start_at=actual.actual_start
                    if actual and actual.actual_start is not None
                    else future_start,
                    end_at=actual.actual_end if completed else future_end,
                    resume_at=future_start if actual is not None and not completed else None,
                    resume_changeover_start=future_begin
                    if actual is not None and not completed
                    else None,
                )
            )
    metrics = []
    units = dict(zip(OBJECTIVES, UNITS))
    for index, name in enumerate(names):
        value = int(solver.value(objective_vars[index])) if has_solution else None
        metrics.append(
            Metric(
                name=name,
                value=value,
                unit=units[name],
                lower_bound=search.lower_bounds[index],
                unknown_reason="This run produced no schedule." if value is None else None,
            )
        )
    effective = max(snapshot.snapshot_clock, origin)
    accept_before = min(
        snapshot.horizon.end_at,
        min(
            (
                a.resume_changeover_start or a.changeover_start
                for a in assignments
                if tasks[a.operation_id].actual is None
                or tasks[a.operation_id].actual.state != "COMPLETED"
            ),
            default=effective,
        )
        + timedelta(minutes=1),
    )
    if snapshot.profile.policy.progress_revalidation_enabled or new_actions_not_before is not None:
        accept_before = min(
            snapshot.horizon.end_at,
            first_changed_occupancy(snapshot, assignments, baseline) + timedelta(minutes=1),
        )
    snapshot_hash = str(snapshot.content_hash)
    candidate = Candidate(
        schema_version="byof.candidate/3"
        if new_actions_not_before is not None or (has_solution and not assignments)
        else "byof.candidate/2",
        new_actions_not_before=new_actions_not_before,
        empty_demand=has_solution and not assignments,
        candidate_id=str(uuid.uuid4()),
        factory_id=snapshot.factory_id,
        version=1,
        binding=VersionBinding(
            snapshot_hash=snapshot_hash,
            planning_revision=snapshot.planning_revision,
            scope_version=snapshot.scope_version,
            profile_version=snapshot.profile.version,
            policy_version=snapshot.profile.policy.policy_version,
            objective_version=objective_version,
            baseline_plan_version=snapshot.active_plan_version,
        ),
        native_status=native,
        last_search_status=last_search_status,
        constant_objective_levels=search.constant_levels,
        solver_passes=search.passes,
        has_solution=has_solution,
        termination_reason="MODEL_ERROR"
        if last_search_status == "MODEL_INVALID"
        else "TIME_LIMIT"
        if search.stopped_for_budget
        else "COMPLETED",
        objective=tuple(metrics),
        proven_objective_levels=search.proven,
        assignments=tuple(assignments),
        scenario=(
            ScenarioFact(
                field="allow_overtime",
                value=True,
                reason="Scenario that may use overtime windows, pending approval",
            ),
        )
        if allow_overtime
        else (),
        required_consents=("allow_overtime",) if allow_overtime else (),
        checker=CheckReport(
            checker_version="not-run", snapshot_hash=snapshot_hash, status="NOT_RUN"
        ),
        effective_not_before=effective,
        accept_before=accept_before,
    )
    from packages.planning.checker import check_candidate

    report = check_candidate(
        snapshot, candidate, baseline=baseline, allow_overtime=allow_overtime, objective=objective
    )
    data = candidate.model_dump(exclude={"content_hash"})
    data["checker"] = report
    candidate = Candidate.model_validate(data)
    evidence.update(
        {
            "solver": "OR-Tools CP-SAT",
            "solver_version": __import__("ortools").__version__,
            "seed": SEED,
            "search_workers": 1,
            "time_limit_seconds": time_limit,
            "hint_operations": len(hint),
            "build_seconds": round(built - started, 4),
            "solver_wall_seconds": sum(p.wall_time_seconds for p in search.passes),
            "native_status": native,
            "last_search_status": last_search_status,
            "solver_passes": [p.model_dump() for p in search.passes],
            "constant_objective_levels": list(search.constant_levels),
            "objective_strategy": "sequential_lexicographic",
            "last_response_stats": search.last_solver.response_stats(),
            "model_validation": model.validate(),
            "snapshot_hash": snapshot_hash,
            "objective_version": objective_version,
            "quality_interpretation": "Future quality gates precede dependent work; execution still requires recorded passing results.",
        }
    )
    evidence["total_elapsed_seconds"] = round(time.perf_counter() - started, 4)
    return candidate, evidence


def solve(
    snapshot: Snapshot,
    *,
    time_limit: float = 30,
    allow_overtime: bool = False,
    baseline: Candidate | None = None,
    objective: EffectiveObjective | None = None,
    new_actions_not_before: datetime | None = None,
) -> Candidate:
    candidate, _ = solve_with_evidence(
        snapshot,
        time_limit=time_limit,
        allow_overtime=allow_overtime,
        baseline=baseline,
        objective=objective,
        new_actions_not_before=new_actions_not_before,
    )
    return candidate
