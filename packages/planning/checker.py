"""Recompute schedule legality from original facts, independently of solver compilation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError

from packages.domain.models import (
    BOM,
    Assignment,
    CalendarWindow,
    Candidate,
    CheckReport,
    EvidenceIssue,
    Metric,
    RouteStep,
    Snapshot,
    TimeWindow,
    canonical_hash,
    duration_minutes,
    minute_offset,
)
from packages.domain.objectives import EffectiveObjective
from packages.planning.execution_projection import (
    ProjectionError,
    project_execution,
    remaining_actions_hash,
)

CHECKER_VERSION = "byof.independent-checker/3"
OBJECTIVE_VERSION = "delivery-v1"
METRIC_NAMES = (
    "weighted_tardiness",
    "incremental_overtime_metric",
    "changed_operations",
    "total_start_shift",
    "makespan",
)
MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class RevalidatedPlan:
    report: CheckReport
    assignments: tuple[Assignment, ...] = ()
    metrics: tuple[Metric, ...] = ()
    remaining_plan_hash: str | None = None


@dataclass(frozen=True)
class _RequiredOperation:
    order_id: str
    product_id: str
    batch_id: str
    quantity: int
    step: RouteStep


def _required_operations(
    snapshot: Snapshot, *, include_cancelled: bool = False
) -> dict[str, _RequiredOperation]:
    """Own expansion: never consume the solver's tasks, demands or compiled constraints."""
    products = {product.product_id: product for product in snapshot.profile.products}
    required = {}
    if snapshot.production_batches is not None:
        for batch in snapshot.production_batches:
            if batch.purpose in {"CANCELLED", "SCRAP"} and not include_cancelled:
                continue
            for step in snapshot.profile.routes:
                if step.product_id == batch.product_id:
                    required[f"{batch.batch_id}-{step.operation_code}"] = _RequiredOperation(
                        batch.order_id, batch.product_id, batch.batch_id, batch.quantity, step
                    )
        return required
    for order in snapshot.orders:
        product = products[order.product_id]
        for index in range(order.quantity // product.batch_size):
            batch_id = f"{order.order_id}-R{order.split_revision:03d}-B{index + 1:03d}"
            for step in snapshot.profile.routes:
                if step.product_id == product.product_id:
                    operation_id = f"{batch_id}-{step.operation_code}"
                    required[operation_id] = _RequiredOperation(
                        order.order_id, product.product_id, batch_id, product.batch_size, step
                    )
    return required


def _whole_minutes(delta: timedelta) -> bool:
    return delta % MINUTE == timedelta(0)


def _overlaps(start: datetime, end: datetime, window: TimeWindow) -> bool:
    return start < window.end_at and window.start_at < end


def _covered(
    start: datetime, end: datetime, calendar: tuple[CalendarWindow, ...], *, allow_overtime: bool
) -> bool:
    cursor = start
    for window in sorted(calendar, key=lambda value: value.start_at):
        if window.kind == "OVERTIME" and not allow_overtime:
            continue
        if window.end_at <= cursor:
            continue
        if window.start_at > cursor:
            return False
        cursor = max(cursor, window.end_at)
        if cursor >= end:
            return True
    return False


def _overtime_minutes(snapshot: Snapshot, assignments: Sequence[Assignment]) -> int:
    workers = {worker.worker_id: worker for worker in snapshot.workers}
    completed = {a.operation_id for a in snapshot.actuals if a.state == "COMPLETED"}
    duration = timedelta(0)
    intervals = [
        (actual.worker_id, segment.start_at, segment.end_at)
        for actual in snapshot.actuals
        for segment in actual.segments
    ]
    for assignment in assignments:
        if assignment.operation_id in completed:
            continue
        start = max(
            assignment.resume_changeover_start or assignment.changeover_start,
            snapshot.snapshot_clock,
        )
        if start < assignment.end_at:
            intervals.append((assignment.worker_id, start, assignment.end_at))
    for worker_id, begin, finish in intervals:
        worker = workers.get(worker_id)
        if worker is None:
            raise ValueError("Cannot compute overtime for an unknown worker")
        for window in worker.calendar:
            if window.kind == "OVERTIME":
                start = max(begin, window.start_at)
                end = min(finish, window.end_at)
                if start < end:
                    duration += end - start
    # The objective uses integer minutes; fractional total overtime rounds upward, never downward.
    return -(-duration // MINUTE)


def calculate_metrics(
    snapshot: Snapshot,
    assignments: Sequence[Assignment],
    *,
    baseline: Candidate | None = None,
) -> tuple[Metric, ...]:
    """Recompute the default objective; this does not certify legality or optimality."""
    required = _required_operations(snapshot)
    by_id = {assignment.operation_id: assignment for assignment in assignments}
    if len(by_id) != len(assignments) or set(by_id) != set(required):
        raise ValueError("Metrics require every original operation exactly once")
    if not assignments and snapshot.production_batches is None:
        raise ValueError("Metrics require a nonempty production scope")
    customer_batches = (
        {batch.batch_id for batch in snapshot.production_batches if batch.purpose == "CUSTOMER"}
        if snapshot.production_batches is not None
        else {operation.batch_id for operation in required.values()}
    )
    order_completion: dict[str, datetime] = {}
    for operation_id, operation in required.items():
        if operation.batch_id not in customer_batches:
            continue
        end = by_id[operation_id].end_at
        order_completion[operation.order_id] = max(
            order_completion.get(operation.order_id, end), end
        )
    weighted_tardiness = sum(
        order.priority_weight
        * max(0, minute_offset(order.due_at, order_completion[order.order_id], round_up=True))
        for order in snapshot.orders
        if order.order_id in order_completion
    )
    overtime = _overtime_minutes(snapshot, assignments)
    changed = shift = 0
    if baseline is not None:
        overtime -= _overtime_minutes(snapshot, baseline.assignments)
        old = {assignment.operation_id: assignment for assignment in baseline.assignments}
        completed = {
            actual.operation_id for actual in snapshot.actuals if actual.state == "COMPLETED"
        }
        for operation_id in (set(old) & set(by_id)) - completed:
            before, after = old[operation_id], by_id[operation_id]
            if (
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
            ):
                changed += 1
            shift += abs(minute_offset(before.start_at, after.start_at, round_up=False))
    makespan = minute_offset(
        snapshot.horizon.start_at,
        max((a.end_at for a in assignments), default=snapshot.horizon.start_at),
        round_up=True,
    )
    values = (weighted_tardiness, overtime, changed, shift, makespan)
    units = ("minutes", "minutes", "operations", "minutes", "minutes")
    return tuple(
        Metric(name=name, value=value, unit=unit)
        for name, value, unit in zip(METRIC_NAMES, values, units)
    )


def check_candidate(
    snapshot: Snapshot,
    candidate: Candidate,
    *,
    baseline: Candidate | None = None,
    allow_overtime: bool = False,
    objective: EffectiveObjective | None = None,
) -> CheckReport:
    """Check complete history plus executable future actions from the original snapshot."""
    issues: list[EvidenceIssue] = []
    digest = snapshot.content_hash or canonical_hash(
        snapshot.model_dump(mode="json", exclude={"content_hash"})
    )

    def issue(code: str, message: str, object_id: str | None = None) -> None:
        issues.append(EvidenceIssue(code=code, message=message, object_id=object_id))

    def report() -> CheckReport:
        return CheckReport(
            checker_version=CHECKER_VERSION,
            snapshot_hash=digest,
            status="FAIL" if issues else "PASS",
            issues=tuple(issues),
        )

    try:
        snapshot = Snapshot.model_validate(snapshot)
        candidate = Candidate.model_validate(candidate)
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False):
            issue("INVALID_CONTRACT", f"{'.'.join(map(str, error['loc']))}: {error['type']}")
        return report()
    digest = snapshot.content_hash or digest
    if objective is not None:
        try:
            objective.validate_for(snapshot)
        except ValueError:
            issue("OBJECTIVE_MISMATCH", "Objective contract differs from the factory configuration")
            return report()
    if type(allow_overtime) is not bool:
        issue("INVALID_SCENARIO", "Overtime permission must be an explicit boolean")
        return report()
    if not candidate.has_solution:
        issue("NO_SOLUTION", "A checker cannot certify a candidate without assigned production")
        return report()
    binding = candidate.binding
    expected_binding = {
        "snapshot_hash": digest,
        "planning_revision": snapshot.planning_revision,
        "scope_version": snapshot.scope_version,
        "profile_version": snapshot.profile.version,
        "policy_version": snapshot.profile.policy.policy_version,
        "objective_version": objective.objective_version
        if objective is not None
        else OBJECTIVE_VERSION,
        "baseline_plan_version": snapshot.active_plan_version,
    }
    for name, expected in expected_binding.items():
        if getattr(binding, name) != expected:
            issue(
                "VERSION_MISMATCH",
                f"Candidate binding {name} differs from original facts",
                candidate.candidate_id,
            )
    if candidate.factory_id != snapshot.factory_id:
        issue("FACTORY_MISMATCH", "Candidate belongs to another factory", candidate.candidate_id)
    if candidate.effective_not_before < snapshot.snapshot_clock:
        issue("STALE_TIME", "Candidate effective time precedes the current business clock")
    if candidate.accept_before <= snapshot.snapshot_clock:
        issue("STALE_TIME", "Candidate acceptance window has expired on the business clock")
    if (
        candidate.new_actions_not_before is not None
        and candidate.new_actions_not_before >= snapshot.horizon.end_at
    ):
        issue("INVALID_ACTION_TIME", "New actions must remain inside the original horizon")
    if allow_overtime:
        if (
            len(candidate.scenario) != 1
            or candidate.scenario[0].field != "allow_overtime"
            or candidate.scenario[0].value is not True
        ):
            issue("UNSUPPORTED_SCENARIO", "Overtime must be the sole explicit supported scenario")
        if "allow_overtime" not in candidate.required_consents:
            issue(
                "MISSING_CONSENT",
                "An overtime scenario requires a manager approval before publication",
            )
    elif candidate.scenario:
        issue("UNSUPPORTED_SCENARIO", "This checker call did not authorize a scenario change")
    supported_consents = {"allow_overtime"} if allow_overtime else set()
    if set(candidate.required_consents) - supported_consents:
        issue("UNSUPPORTED_CONSENT", "Candidate requests a consent outside this supported scenario")

    return _check_physical_plan(
        snapshot,
        candidate.assignments,
        baseline=baseline,
        allow_overtime=allow_overtime,
        objective=objective,
        effective_not_before=candidate.effective_not_before,
        new_actions_not_before=candidate.new_actions_not_before,
        claimed_metrics=candidate.objective,
        initial_issues=issues,
    )


def _check_physical_plan(
    snapshot: Snapshot,
    plan_assignments: Sequence[Assignment],
    *,
    baseline: Candidate | None,
    allow_overtime: bool,
    objective: EffectiveObjective | None,
    effective_not_before: datetime,
    new_actions_not_before: datetime | None = None,
    claimed_metrics: Sequence[Metric] | None = None,
    initial_issues: Sequence[EvidenceIssue] = (),
) -> CheckReport:
    """Check actual history and the full future scope without granting version or time waivers."""
    issues = list(initial_issues)
    digest = str(snapshot.content_hash)
    objective_names = objective.order if objective is not None else METRIC_NAMES

    def issue(code: str, message: str, object_id: str | None = None) -> None:
        issues.append(EvidenceIssue(code=code, message=message, object_id=object_id))

    def report() -> CheckReport:
        return CheckReport(
            checker_version=CHECKER_VERSION,
            snapshot_hash=digest,
            status="FAIL" if issues else "PASS",
            issues=tuple(issues),
        )

    if snapshot.actuals and snapshot.schema_version not in {"byof.snapshot/2", "byof.snapshot/3"}:
        issue(
            "UNSUPPORTED_WIP",
            "Legacy execution lacks versioned reservations and actual segments",
        )
    if baseline is not None or snapshot.active_plan_version is not None or snapshot.actuals:
        if (
            snapshot.schema_version not in {"byof.snapshot/2", "byof.snapshot/3"}
            or baseline is None
            or snapshot.active_plan_version is None
            or snapshot.active_plan_hash is None
        ):
            issue("UNSUPPORTED_BASELINE", "Replanning requires the source-accepted active plan")
        else:
            try:
                baseline = Candidate.model_validate(baseline)
                if (
                    baseline.content_hash != snapshot.active_plan_hash
                    or baseline.factory_id != snapshot.factory_id
                    or not baseline.has_solution
                ):
                    issue(
                        "BASELINE_MISMATCH", "Baseline differs from the source's active candidate"
                    )
            except ValidationError:
                issue("BASELINE_MISMATCH", "Active baseline evidence is invalid")
    if snapshot.production_batches is None and any(
        order.status not in {"CONFIRMED", "IN_PROGRESS", "COMPLETED"} for order in snapshot.orders
    ):
        issue(
            "UNSUPPORTED_ORDER_STATE",
            "Cancelled orders require explicit history and inventory reconciliation",
        )
    if not snapshot.orders:
        issue("EMPTY_SCOPE", "There are no confirmed production orders")
    if issues:
        return report()
    if (
        not snapshot.source.complete
        or snapshot.source.consistency == "UNVERIFIED"
        or snapshot.source.freshness != "CURRENT"
    ):
        issue(
            "SOURCE_NOT_CURRENT",
            "Planning requires complete current facts with a verified source watermark",
        )
    for receipt in snapshot.receipts:
        if receipt.received_at is not None and receipt.received_at > snapshot.snapshot_clock:
            issue(
                "FUTURE_ACTUAL_RECEIPT",
                "A future receipt cannot already be an actual source fact",
                receipt.receipt_id,
            )
    required = _required_operations(snapshot)
    historical_required = _required_operations(snapshot, include_cancelled=True)
    assignments = {assignment.operation_id: assignment for assignment in plan_assignments}
    if baseline is not None and {a.operation_id for a in baseline.assignments} - set(
        _required_operations(snapshot, include_cancelled=True)
    ):
        issue(
            "BASELINE_SCOPE_MISMATCH",
            "Operations from the active plan disappeared without a supported cancellation or split reconciliation",
        )
    for missing in sorted(set(required) - set(assignments)):
        issue("MISSING_OPERATION", "Required operation is absent from the candidate", missing)
    for extra in sorted(set(assignments) - set(required)):
        issue(
            "EXTRA_OPERATION", "Candidate contains an operation outside the original scope", extra
        )
    if set(assignments) != set(required):
        return report()

    resources = {resource.resource_id: resource for resource in snapshot.resources}
    workers = {worker.worker_id: worker for worker in snapshot.workers}
    by_resource: dict[str, list[Assignment]] = defaultdict(list)
    by_worker: dict[str, list[Assignment]] = defaultdict(list)
    by_batch_step = {
        (operation.batch_id, operation.step.step_id): operation_id
        for operation_id, operation in required.items()
    }
    earliest = max(snapshot.snapshot_clock, snapshot.horizon.start_at, effective_not_before)
    actuals = {actual.operation_id: actual for actual in snapshot.actuals}
    for owner_field, code in (
        ("resource_id", "ACTUAL_RESOURCE_CONFLICT"),
        ("worker_id", "ACTUAL_WORKER_CONFLICT"),
    ):
        history: dict[str, list[tuple[datetime, datetime, str]]] = defaultdict(list)
        for historical in snapshot.actuals:
            for segment in historical.segments:
                history[getattr(historical, owner_field)].append(
                    (segment.start_at, segment.end_at, historical.operation_id)
                )
        for owner_id, intervals in history.items():
            previous_end: datetime | None = None
            for start, end, operation_id in sorted(intervals):
                if previous_end is not None and start < previous_end:
                    issue(code, f"Source execution overlaps on {owner_id}", operation_id)
                previous_end = max(end, previous_end) if previous_end is not None else end
    future: dict[str, tuple[datetime, datetime]] = {}
    baseline_assignments = {a.operation_id: a for a in baseline.assignments} if baseline else {}
    frozen_until = snapshot.snapshot_clock + snapshot.profile.policy.freeze_window_min * MINUTE
    from packages.planning.disruption import recovery_operations

    recovery = recovery_operations(snapshot, baseline) if baseline is not None else set()
    known_assignments = True
    for operation_id, assignment in assignments.items():
        operation = required[operation_id]
        step = operation.step
        actual = actuals.get(operation_id)
        for predecessor in step.predecessors:
            previous = assignments[by_batch_step[operation.batch_id, predecessor]]
            if previous.end_at > assignment.start_at:
                predecessor_step = required[previous.operation_id].step
                code = "QUALITY_PRECEDENCE" if predecessor_step.quality_gate else "PRECEDENCE"
                issue(
                    code,
                    f"Predecessor {previous.operation_id} must finish before this operation",
                    operation_id,
                )
        if actual is not None:
            if (
                assignment.resource_id != actual.resource_id
                or assignment.worker_id != actual.worker_id
                or assignment.changeover_start != actual.changeover_start
                or (actual.actual_start is not None and assignment.start_at != actual.actual_start)
            ):
                issue(
                    "ACTUAL_HISTORY_CHANGED",
                    "Candidate changes the source's actual start, setup, equipment or employee",
                    operation_id,
                )
            if actual.state == "COMPLETED":
                if step.quality_gate and actual.quality_state != "PASSED":
                    issue(
                        "QUALITY_CONFIRMATION_REQUIRED",
                        "Completed inspection lacks a confirmed passing result",
                        operation_id,
                    )
                if (
                    assignment.end_at != actual.actual_end
                    or assignment.resume_at is not None
                    or assignment.resume_changeover_start is not None
                ):
                    issue(
                        "ACTUAL_HISTORY_CHANGED",
                        "Completed history must remain exact and cannot resume",
                        operation_id,
                    )
                continue
            if assignment.resume_at is None or assignment.resume_changeover_start is None:
                issue(
                    "MISSING_CONTINUATION",
                    "Incomplete actual work requires explicit future setup and production",
                    operation_id,
                )
                continue
            occupancy_start, production_start = (
                assignment.resume_changeover_start,
                assignment.resume_at,
            )
            if actual.actual_start is None and assignment.start_at != production_start:
                issue(
                    "ACTUAL_HISTORY_CHANGED",
                    "Initial setup has no historical production start",
                    operation_id,
                )
            if actual.remaining_minutes is None or actual.remaining_setup_minutes is None:
                issue(
                    "CONFIRMATION_REQUIRED",
                    "Remaining production and setup work must be confirmed by the source",
                    operation_id,
                )
            elif assignment.end_at - production_start != actual.remaining_minutes * MINUTE:
                issue(
                    "WRONG_REMAINING_DURATION",
                    "Continuation differs from confirmed remaining production",
                    operation_id,
                )
            if (
                actual.state in {"IN_PROGRESS", "SETUP"}
                and occupancy_start != snapshot.snapshot_clock
            ):
                issue(
                    "ACTIVE_WORK_MOVED",
                    "Unblocked execution must continue at the current business clock",
                    operation_id,
                )
        else:
            occupancy_start, production_start = assignment.changeover_start, assignment.start_at
            if assignment.resume_at is not None or assignment.resume_changeover_start is not None:
                issue(
                    "UNEXPECTED_CONTINUATION",
                    "A task without actual history cannot resume",
                    operation_id,
                )
            if (
                assignment.end_at - production_start
                != duration_minutes(step, operation.quantity) * MINUTE
            ):
                issue(
                    "WRONG_DURATION",
                    "Duration differs from original setup/cycle/quantity calculation",
                    operation_id,
                )
            previous_plan = baseline_assignments.get(operation_id)
            if (
                previous_plan is not None
                and operation_id not in recovery
                and snapshot.snapshot_clock <= previous_plan.start_at < frozen_until
            ):
                if (assignment.start_at, assignment.resource_id, assignment.worker_id) != (
                    previous_plan.start_at,
                    previous_plan.resource_id,
                    previous_plan.worker_id,
                ):
                    issue(
                        "FROZEN_OPERATION_CHANGED",
                        "Near-term unstarted work retains its approved start, equipment and employee",
                        operation_id,
                    )
        if new_actions_not_before is not None:
            previous_plan = baseline_assignments.get(operation_id)
            if actual is None:
                protected = (
                    previous_plan is not None
                    and operation_id not in recovery
                    and snapshot.snapshot_clock
                    <= previous_plan.changeover_start
                    < new_actions_not_before
                )
                if protected and assignment != previous_plan:
                    issue(
                        "REVIEW_PREFIX_CHANGED",
                        "Previously authorized dispatch inside the review interval must remain exact",
                        operation_id,
                    )
                elif not protected and occupancy_start < new_actions_not_before:
                    issue(
                        "NEW_ACTION_TOO_EARLY",
                        "Changed dispatch precedes the candidate's explicit earliest new action time",
                        operation_id,
                    )
            elif (
                actual.state not in {"SETUP", "IN_PROGRESS"}
                and occupancy_start < new_actions_not_before
            ):
                issue(
                    "NEW_ACTION_TOO_EARLY",
                    "Restart of interrupted work precedes the explicit earliest new action time",
                    operation_id,
                )
        future[operation_id] = occupancy_start, production_start
        if any(
            not _whole_minutes(value - snapshot.horizon.start_at)
            for value in (occupancy_start, production_start, assignment.end_at)
        ):
            issue(
                "INVALID_TIME_GRID",
                "Future occupancy must use integer minutes from the planning origin",
                operation_id,
            )
        if occupancy_start < earliest or assignment.end_at > snapshot.horizon.end_at:
            issue(
                "OUTSIDE_PLANNING_WINDOW",
                "Future occupancy must fit the current executable horizon",
                operation_id,
            )
        resource = resources.get(assignment.resource_id)
        worker = workers.get(assignment.worker_id)
        if resource is None or worker is None:
            issue(
                "UNKNOWN_ASSIGNMENT",
                "Assignment references an unknown resource or worker",
                operation_id,
            )
            known_assignments = False
            continue
        if (
            resource.resource_type != step.resource_type
            or step.operation_code not in resource.operation_codes
        ):
            issue(
                "RESOURCE_QUALIFICATION",
                "Resource is not qualified for this original routing step",
                operation_id,
            )
        if step.skill not in worker.skills:
            issue("WORKER_QUALIFICATION", "Employee lacks the required skill", operation_id)
        if resource.status != "AVAILABLE":
            issue("RESOURCE_UNAVAILABLE", "Resource state is not confirmed available", operation_id)
        if worker.status != "AVAILABLE":
            issue("WORKER_UNAVAILABLE", "Employee state is not confirmed available", operation_id)
        for name, owner, permit_overtime in (
            ("RESOURCE", resource, allow_overtime),
            ("WORKER", worker, allow_overtime and worker.overtime_available),
        ):
            if not _covered(
                occupancy_start, assignment.end_at, owner.calendar, allow_overtime=permit_overtime
            ):
                issue(
                    f"{name}_CALENDAR",
                    "Future changeover and work must fit continuous qualified availability",
                    operation_id,
                )
            if any(
                _overlaps(occupancy_start, assignment.end_at, unavailable)
                for unavailable in owner.unavailable
            ):
                issue(
                    f"{name}_UNAVAILABLE_INTERVAL",
                    "Future occupied interval overlaps known unavailability",
                    operation_id,
                )
        by_resource[resource.resource_id].append(assignment)
        by_worker[worker.worker_id].append(assignment)

    for resource_id, sequence in by_resource.items():
        previous_assignment: Assignment | None = None
        resource = resources[resource_id]
        if (
            resource.last_operation_id in required
            and resource.last_product_id != required[resource.last_operation_id].product_id
        ):
            issue(
                "SOURCE_SETUP_STATE",
                "Equipment setup product differs from its referenced operation",
                resource_id,
            )
        if (
            resource.last_operation_id in actuals
            and actuals[resource.last_operation_id].resource_id != resource_id
        ):
            issue(
                "SOURCE_SETUP_STATE",
                "Equipment setup points to execution on another resource",
                resource_id,
            )
        if resource.last_operation_id is None and any(
            a.resource_id == resource_id for a in snapshot.actuals
        ):
            issue(
                "SOURCE_SETUP_STATE",
                "A used machine must identify its current setup state",
                resource_id,
            )
        for assignment in sorted(
            sequence, key=lambda item: (future[item.operation_id][1], item.operation_id)
        ):
            actual = actuals.get(assignment.operation_id)
            if previous_assignment is None:
                if resource.last_operation_id == assignment.operation_id and actual is not None:
                    expected = actual.remaining_setup_minutes
                elif resource.last_product_id is not None:
                    expected = (
                        snapshot.profile.policy.same_product_changeover_min
                        if resource.last_product_id == required[assignment.operation_id].product_id
                        else snapshot.profile.policy.different_product_changeover_min
                    )
                else:
                    expected = snapshot.profile.policy.first_changeover_min
            elif (
                required[previous_assignment.operation_id].product_id
                == required[assignment.operation_id].product_id
            ):
                expected = snapshot.profile.policy.same_product_changeover_min
            else:
                expected = snapshot.profile.policy.different_product_changeover_min
            occupancy_start, production_start = future[assignment.operation_id]
            if expected is not None and production_start - occupancy_start != expected * MINUTE:
                issue(
                    "CHANGEOVER",
                    f"Resource {resource_id} requires exactly {expected} minutes before work",
                    assignment.operation_id,
                )
            previous_assignment = assignment
    for name, groups in (("RESOURCE", by_resource), ("WORKER", by_worker)):
        for owner_id, sequence in groups.items():
            latest_end: datetime | None = None
            previous_id: str | None = None
            for assignment in sorted(
                sequence, key=lambda item: (future[item.operation_id][0], item.operation_id)
            ):
                if latest_end is not None and future[assignment.operation_id][0] < latest_end:
                    issue(
                        f"{name}_CONFLICT",
                        f"{owner_id} overlaps {previous_id}, including changeover",
                        assignment.operation_id,
                    )
                if latest_end is None or assignment.end_at > latest_end:
                    latest_end, previous_id = assignment.end_at, assignment.operation_id

    demand: dict[str, list[tuple[datetime, int, str]]] = defaultdict(list)
    bom_by_product: dict[str, list[BOM]] = defaultdict(list)
    for item in snapshot.profile.bom:
        bom_by_product[item.product_id].append(item)
    started_batches = {a.batch_id for a in snapshot.actuals if a.actual_start is not None}
    if snapshot.schema_version in {"byof.snapshot/2", "byof.snapshot/3"}:
        # Reconcile original per-operation consumption with the unconsumed physical kit.
        remaining: dict[tuple[str, str], int] = {}
        used: dict[tuple[str, str], int] = defaultdict(int)
        for reservation in snapshot.reservations:
            key = reservation.batch_id, reservation.material_id
            if key in remaining:
                issue(
                    "RESERVATION_CONFLICT",
                    "A batch material is physically reserved more than once",
                    reservation.batch_id,
                )
            remaining[key] = reservation.quantity
        for actual in snapshot.actuals:
            operation = historical_required[actual.operation_id]
            expected_consumed = {
                item.material_id: item.quantity_per_unit * operation.quantity
                for item in bom_by_product[operation.product_id]
                if actual.actual_start is not None
                and item.consume_step_id == operation.step.step_id
            }
            consumed = {item.material_id: item.quantity for item in actual.consumed}
            if consumed != expected_consumed:
                issue(
                    "ACTUAL_MATERIAL_CONFLICT",
                    "Actual consumption differs from the original operation BOM",
                    actual.operation_id,
                )
            for material, quantity in consumed.items():
                used[actual.batch_id, material] += quantity
        for operation in historical_required.values():
            if operation.step.predecessors or operation.batch_id not in started_batches:
                continue
            for item in bom_by_product[operation.product_id]:
                key = operation.batch_id, item.material_id
                if (
                    key not in remaining
                    or remaining[key] + used[key] != item.quantity_per_unit * operation.quantity
                ):
                    issue(
                        "RESERVATION_CONFLICT",
                        "Started batch material must remain consumed or physically reserved",
                        operation.batch_id,
                    )
        for stock in snapshot.inventory:
            if (
                sum(
                    quantity
                    for (_, material), quantity in remaining.items()
                    if material == stock.material_id
                )
                > stock.reserved
            ):
                issue(
                    "RESERVATION_CONFLICT",
                    "Owned batch reservations exceed physical reserved stock",
                    stock.material_id,
                )
    for operation_id, operation in required.items():
        if not operation.step.predecessors and operation.batch_id not in started_batches:
            for item in bom_by_product[operation.product_id]:
                demand[item.material_id].append(
                    (
                        assignments[operation_id].start_at,
                        item.quantity_per_unit * operation.quantity,
                        operation.batch_id,
                    )
                )
    inventory = {item.material_id: item for item in snapshot.inventory}
    for material_id, requests in demand.items():
        balance = inventory[material_id].on_hand - inventory[material_id].reserved
        incoming = sorted(
            (receipt.eta, receipt.receipt_id, receipt.quantity)
            for receipt in snapshot.receipts
            if receipt.material_id == material_id and receipt.status == "CONFIRMED"
        )
        index = 0
        for at, quantity, batch_id in sorted(requests):
            while index < len(incoming) and incoming[index][0] <= at:
                balance += incoming[index][2]
                index += 1
            balance -= quantity
            if balance < 0:
                issue(
                    "MATERIAL_SHORTAGE",
                    f"{batch_id} lacks {-balance} {inventory[material_id].unit} at full-kit start {at.isoformat()}",
                    material_id,
                )

    customer_batches = (
        {batch.batch_id for batch in snapshot.production_batches if batch.purpose == "CUSTOMER"}
        if snapshot.production_batches is not None
        else {operation.batch_id for operation in required.values()}
    )
    for batch in snapshot.production_batches or ():
        if batch.purpose == "CUSTOMER" and batch.delivery_due_at is not None:
            if any(
                assignments[operation_id].end_at > batch.delivery_due_at
                for operation_id, operation in required.items()
                if operation.batch_id == batch.batch_id
            ):
                issue(
                    "BATCH_DELIVERY_DEADLINE",
                    "Batch exceeds its accepted partial-delivery commitment",
                    batch.batch_id,
                )
    for order in snapshot.orders:
        ends = [
            assignments[operation_id].end_at
            for operation_id, operation in required.items()
            if operation.order_id == order.order_id and operation.batch_id in customer_batches
        ]
        if not ends:
            continue
        completion = max(ends)
        if order.hard_deadline and completion > order.due_at:
            issue(
                "HARD_DEADLINE",
                "Order completion exceeds the unchanged hard commitment",
                order.order_id,
            )
    if known_assignments or not required:
        metrics = calculate_metrics(snapshot, plan_assignments, baseline=baseline)
        by_name = {metric.name: metric for metric in metrics}
        expected_metrics = tuple(
            (name, by_name[name].value, by_name[name].unit) for name in objective_names
        )
        if objective is not None:
            for name, bound in objective.bounds.items():
                value = by_name[name].value
                if value is not None and value > bound:
                    issue(
                        "OBJECTIVE_BOUND_EXCEEDED",
                        "Independently recomputed objective exceeds its confirmed upper bound",
                        name,
                    )
        actual_metrics = (
            tuple((metric.name, metric.value, metric.unit) for metric in claimed_metrics)
            if claimed_metrics is not None
            else None
        )
        if actual_metrics is not None and actual_metrics != expected_metrics:
            issue(
                "METRIC_MISMATCH",
                "Objective vector differs from independently recomputed business metrics",
            )
        for metric in claimed_metrics or ():
            if (
                metric.value is not None
                and metric.lower_bound is not None
                and metric.lower_bound > metric.value
            ):
                issue(
                    "INVALID_OBJECTIVE_BOUND",
                    "A minimization lower bound cannot exceed the candidate value",
                    metric.name,
                )
    return report()


def check_revalidated_plan(
    original_snapshot: Snapshot,
    current_snapshot: Snapshot,
    candidate: Candidate,
    *,
    baseline: Candidate,
    objective: EffectiveObjective | None = None,
) -> RevalidatedPlan:
    """Check immutable approved intent against new actuals; event-chain evidence is also required."""
    digest = str(current_snapshot.content_hash)

    def failed(code: str, object_id: str | None = None) -> RevalidatedPlan:
        return RevalidatedPlan(
            report=CheckReport(
                checker_version=CHECKER_VERSION,
                snapshot_hash=digest,
                status="FAIL",
                issues=(
                    EvidenceIssue(
                        code=code,
                        message="Approved work cannot be retained across these facts",
                        object_id=object_id,
                    ),
                ),
            )
        )

    try:
        original = Snapshot.model_validate(original_snapshot)
        current = Snapshot.model_validate(current_snapshot)
        candidate = Candidate.model_validate(candidate)
        baseline = Candidate.model_validate(baseline)
    except ValueError:
        return failed("INVALID_CONTRACT")
    if (
        not original.profile.policy.progress_revalidation_enabled
        or not current.profile.policy.progress_revalidation_enabled
    ):
        return failed("PROGRESS_REVALIDATION_DISABLED")
    if original.schema_version not in {
        "byof.snapshot/2",
        "byof.snapshot/3",
    } or current.schema_version not in {"byof.snapshot/2", "byof.snapshot/3"}:
        return failed("REVALIDATION_LEDGER_REQUIRED")
    if (
        original.active_plan_version is None
        or original.active_plan_version != current.active_plan_version
        or original.active_plan_hash != current.active_plan_hash
        or baseline.content_hash != original.active_plan_hash
        or baseline.factory_id != original.factory_id
        or not baseline.has_solution
    ):
        return failed("BASELINE_MISMATCH")
    allow_overtime = "allow_overtime" in candidate.required_consents
    original_report = check_candidate(
        original, candidate, baseline=baseline, allow_overtime=allow_overtime, objective=objective
    )
    if original_report.status != "PASS":
        return RevalidatedPlan(
            report=CheckReport(
                checker_version=CHECKER_VERSION,
                snapshot_hash=digest,
                status="FAIL",
                issues=original_report.issues,
            )
        )
    if not candidate.effective_not_before <= current.snapshot_clock < candidate.accept_before:
        return failed("STALE_TIME")
    if objective is not None:
        try:
            objective.validate_for(current)
        except ValueError:
            return failed("OBJECTIVE_MISMATCH")
    try:
        assignments = project_execution(original, current, candidate, baseline=baseline)
    except ProjectionError as exc:
        return failed(exc.code, exc.object_id)
    except ValueError:
        return failed("INVALID_EXECUTION_PROJECTION")
    report = _check_physical_plan(
        current,
        assignments,
        baseline=baseline,
        allow_overtime=allow_overtime,
        objective=objective,
        effective_not_before=current.snapshot_clock,
        new_actions_not_before=candidate.new_actions_not_before,
    )
    if report.status != "PASS":
        return RevalidatedPlan(report=report)
    recalculated = {
        metric.name: metric for metric in calculate_metrics(current, assignments, baseline=baseline)
    }
    names = objective.order if objective is not None else METRIC_NAMES
    return RevalidatedPlan(
        report=report,
        assignments=assignments,
        metrics=tuple(recalculated[name] for name in names),
        remaining_plan_hash=remaining_actions_hash(current, assignments),
    )
