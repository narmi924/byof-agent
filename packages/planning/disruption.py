"""Derive which old dispatches are physically broken, never an authority to execute."""

from collections import defaultdict

from packages.domain.models import Candidate, Snapshot, batch_operations


def recovery_operations(snapshot: Snapshot, baseline: Candidate | None) -> set[str]:
    if baseline is None:
        return set()
    actuals = {a.operation_id: a for a in snapshot.actuals}
    resources = {r.resource_id: r for r in snapshot.resources}
    workers = {w.worker_id: w for w in snapshot.workers}
    live = {row.operation_id for row in batch_operations(snapshot)[1]}
    cancelled = {
        row.operation_id for row in batch_operations(snapshot, include_cancelled=True)[1]
    } - live
    pending = {
        a.operation_id: a
        for a in baseline.assignments
        if a.operation_id not in cancelled
        and (a.operation_id not in actuals or actuals[a.operation_id].state != "COMPLETED")
    }
    affected = set()
    for identity, planned in pending.items():
        actual = actuals.get(identity)
        if actual is not None:
            if (
                actual.state == "BLOCKED"
                and actual.remaining_minutes is not None
                and actual.remaining_setup_minutes is not None
                and actual.remaining_confirmed_by
            ):
                affected.add(identity)
            continue
        begin = planned.resume_changeover_start or planned.changeover_start
        owners = (resources.get(planned.resource_id), workers.get(planned.worker_id))
        if begin < snapshot.snapshot_clock or any(
            owner is not None
            and (
                owner.status != "AVAILABLE"
                or any(w.start_at < planned.end_at and begin < w.end_at for w in owner.unavailable)
            )
            for owner in owners
        ):
            affected.add(identity)
    # Causal successors: route dependencies and later uses of the same equipment/person.
    edges: dict[str, set[str]] = defaultdict(set)
    _, operations = batch_operations(snapshot)
    by_step = {(o.batch_id, o.step_id): o.operation_id for o in operations}
    routes = {step.step_id: step for step in snapshot.profile.routes}
    for operation in operations:
        for predecessor in routes[operation.step_id].predecessors:
            edges[by_step[operation.batch_id, predecessor]].add(operation.operation_id)
    for field in ("resource_id", "worker_id"):
        groups: dict[str, list] = defaultdict(list)
        for assignment in pending.values():
            groups[getattr(assignment, field)].append(assignment)
        for rows in groups.values():
            rows.sort(
                key=lambda a: (a.resume_changeover_start or a.changeover_start, a.operation_id)
            )
            for previous, following in zip(rows, rows[1:]):
                edges[previous.operation_id].add(following.operation_id)
    queue = list(affected)
    while queue:
        for successor in edges[queue.pop()]:
            if successor in pending and successor not in affected:
                # Work that has already started retains its physical continuity constraints.
                if successor in actuals and actuals[successor].state != "BLOCKED":
                    continue
                affected.add(successor)
                queue.append(successor)
    return affected
