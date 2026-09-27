"""Source-backed next actions when a production schedule cannot yet be approved.

These are conditional recovery routes, not fabricated production candidates. Only a
Checker-passing candidate can receive the manager's final execution approval.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict

from packages.agent.planning_context import material_shortfalls
from packages.domain.models import CalendarWindow, Candidate, Snapshot


class RecoveryStep(TypedDict):
    owner: Literal["Manager", "Shop floor", "Agent"]
    action: str


class RecoveryPath(TypedDict):
    path_id: str
    kind: str
    title: str
    evidence: str
    steps: list[RecoveryStep]
    prompt: str


def _path(
    kind: str, title: str, evidence: str, steps: list[RecoveryStep], prompt: str
) -> RecoveryPath:
    return {
        "path_id": "recovery-" + kind,
        "kind": kind,
        "title": title,
        "evidence": evidence,
        "steps": steps,
        "prompt": prompt,
    }


def _covered(start: datetime, end: datetime, windows: tuple[CalendarWindow, ...]) -> bool:
    cursor = start
    for window in sorted(windows, key=lambda row: row.start_at):
        if window.end_at <= cursor:
            continue
        if window.start_at > cursor:
            return False
        cursor = max(cursor, window.end_at)
        if cursor >= end:
            return True
    return False


def _lost_capacity_windows(snapshot: Snapshot, baseline: Candidate | None) -> list[str]:
    if baseline is None:
        return []
    resources = {row.resource_id: row for row in snapshot.resources}
    workers = {row.worker_id: row for row in snapshot.workers}
    affected: set[str] = set()
    for assignment in baseline.assignments:
        if assignment.end_at <= snapshot.snapshot_clock:
            continue
        start = assignment.resume_changeover_start or assignment.changeover_start
        for owner in (
            resources.get(assignment.resource_id),
            workers.get(assignment.worker_id),
        ):
            if owner is None or owner.status != "AVAILABLE":
                continue
            if not _covered(start, assignment.end_at, owner.calendar):
                affected.add(assignment.operation_id)
    return sorted(affected)


def recovery_paths(
    snapshot: Snapshot,
    baseline: Candidate | None = None,
    *,
    solver_state: Literal["INFEASIBLE", "UNKNOWN", "FAILED"] | None = None,
) -> list[RecoveryPath]:
    """Give a responsible next action for every supported adverse source state.

    A solver timeout is never described as proof of physical impossibility. Source
    changes must be confirmed in the factory before a new candidate is approvable.
    """
    paths: list[RecoveryPath] = []
    gaps = material_shortfalls(snapshot)
    if gaps:
        evidence = "; ".join(
            f"{gap['material_name']} short by at least {gap['minimum_shortfall']} {gap['unit']}"
            for gap in gaps
        )
        paths.append(
            _path(
                "material_supply",
                "Cover the verified material shortfall",
                evidence,
                [
                    {
                        "owner": "Manager",
                        "action": "Choose purchasing, a transfer or a confirmed supplier expedite, and check due dates and cost.",
                    },
                    {
                        "owner": "Shop floor",
                        "action": "Record the confirmed receipt quantity and time on the factory side; confirm stock only after the goods arrive.",
                    },
                    {
                        "owner": "Agent",
                        "action": "Recalculate after new shop floor facts arrive and submit an executable plan for manager approval.",
                    },
                ],
                "Assess resupply or transfer for the verified material shortfall. Do not assume purchasing is done; reschedule automatically once the shop floor confirms the receipt, and give me a plan to approve.",
            )
        )
        paths.append(
            _path(
                "material_customer_terms",
                "Confirm adjustable order commitments with customers",
                evidence,
                [
                    {
                        "owner": "Manager",
                        "action": "If supply cannot be secured in time, confirm quantity or due date changes with the affected customers.",
                    },
                    {
                        "owner": "Shop floor",
                        "action": "Enter only order changes that were actually agreed on the factory side.",
                    },
                    {
                        "owner": "Agent",
                        "action": "Recompute the remaining commitments from the new order facts and give a new plan to approve.",
                    },
                ],
                "If resupply cannot be confirmed in time, analyze the affected orders and negotiable due dates or quantities; reschedule after the customer confirms and the shop floor updates the facts.",
            )
        )

    blocked = [row for row in snapshot.actuals if row.state == "BLOCKED"]
    missing = [
        row.operation_id
        for row in blocked
        if row.remaining_minutes is None or row.remaining_setup_minutes is None
    ]
    if missing:
        paths.append(
            _path(
                "confirm_wip",
                "Check the remaining work of interrupted operations",
                f"Remaining production or changeover time of {len(missing)} operations is not confirmed: {', '.join(missing[:3])}",
                [
                    {
                        "owner": "Shop floor",
                        "action": "Measure the remaining production and changeover time on the shop floor and confirm it on the blocked operation.",
                    },
                    {
                        "owner": "Agent",
                        "action": "Keep the executed history and reschedule from the confirmed remaining work.",
                    },
                    {
                        "owner": "Manager",
                        "action": "Review the new resource and due date schedule.",
                    },
                ],
                "Follow up on verifying the remaining work of the blocked operations; after the shop floor confirms, keep the executed part and reschedule.",
            )
        )
    scrapped = {
        batch.batch_id for batch in snapshot.production_batches or () if batch.purpose == "SCRAP"
    }
    failed_quality = [
        row.operation_id
        for row in snapshot.actuals
        if row.quality_state == "FAILED" and row.batch_id not in scrapped
    ]
    if failed_quality:
        paths.append(
            _path(
                "quality_recovery",
                "Isolate failed goods and complete the quality disposition",
                f"{len(failed_quality)} completed operations failed inspection: {', '.join(failed_quality[:3])}",
                [
                    {
                        "owner": "Shop floor",
                        "action": "Isolate the affected batches and recheck; update the result once passed. If unrecoverable, record the scrap basis and the factory creates remake batches.",
                    },
                    {
                        "owner": "Manager",
                        "action": "Approve the remake schedule; if capacity or material is short, confirm resupply, subcontracting or a due date negotiation with the customer.",
                    },
                    {
                        "owner": "Agent",
                        "action": "Reschedule from the updated inspection or scrap facts and submit a plan for review.",
                    },
                ],
                "Follow up on the recheck of the failed batches; if unrecoverable, wait for the shop floor to record the scrap and create remake batches, then reschedule with the latest material and capacity. Do not count the batch as delivered without proof that it passed.",
            )
        )
    unavailable_resources = sorted(
        row.resource_id for row in snapshot.resources if row.status != "AVAILABLE"
    )
    if unavailable_resources:
        paths.append(
            _path(
                "equipment_recovery",
                "Adjust the schedule of machines that are down",
                f"Machines currently unavailable: {', '.join(unavailable_resources)}",
                [
                    {
                        "owner": "Agent",
                        "action": "First check other qualified machines and free windows; if rescheduling works, submit the plan directly.",
                    },
                    {
                        "owner": "Shop floor",
                        "action": "Confirm when repairs or external capacity are actually available; update the shop floor once the machine is back.",
                    },
                    {
                        "owner": "Manager",
                        "action": "Approve reassignment, overtime or subcontracting; if due dates still cannot be met, negotiate with the customer first.",
                    },
                ],
                "Reschedule on the existing qualified machines first; if that is not enough, propose conditions and owners for repair, subcontracting or due date negotiation, and continue solving after the shop floor confirms.",
            )
        )
    absent_workers = sorted(row.worker_id for row in snapshot.workers if row.status != "AVAILABLE")
    if absent_workers:
        paths.append(
            _path(
                "workforce_recovery",
                "Close the capacity gap caused by absence",
                f"Staff currently unavailable: {', '.join(absent_workers)}",
                [
                    {
                        "owner": "Agent",
                        "action": "Check the qualifications and shifts of other staff and reschedule the work they can cover first.",
                    },
                    {
                        "owner": "Shop floor",
                        "action": "Update the shop floor after confirming the actual availability of returning or replacement staff.",
                    },
                    {
                        "owner": "Manager",
                        "action": "Approve the necessary overtime; if it still cannot be covered, negotiate delivery terms.",
                    },
                ],
                "Check qualified cover staff and reschedule; if labor is still short, explain the executable conditions for overtime, return or due date negotiation.",
            )
        )
    lost_windows = _lost_capacity_windows(snapshot, baseline)
    if lost_windows:
        paths.append(
            _path(
                "capacity_window_recovery",
                "Revise the schedule that lost its available windows",
                f"{len(lost_windows)} unfinished operations of the original plan no longer fall within the current available windows.",
                [
                    {
                        "owner": "Agent",
                        "action": "Keep the executed part and move work to regular windows that are still available first.",
                    },
                    {
                        "owner": "Shop floor",
                        "action": "If new overtime windows are really available, confirm and record them on the factory side first.",
                    },
                    {
                        "owner": "Manager",
                        "action": "Review the new schedule; approve overtime explicitly when needed.",
                    },
                ],
                "The windows the original schedule needs are no longer available. Reschedule within the current shifts first; if overtime must be restored, verify the available windows on the shop floor before submitting it for my approval.",
            )
        )
    if solver_state == "UNKNOWN" or solver_state == "FAILED":
        paths.append(
            _path(
                "retry_computation",
                "Keep solving and check the current conditions",
                "This calculation found no usable plan; try again with different conditions.",
                [
                    {
                        "owner": "Agent",
                        "action": "Recheck the constraints and known feasible schedules against the current facts and keep solving.",
                    },
                    {
                        "owner": "Manager",
                        "action": "If delivery priorities changed, state the business goal; there is no need to re-enter shop floor facts.",
                    },
                ],
                "This calculation has no checkable plan yet; keep solving from the current facts and report progress. Do not treat a timeout as proven infeasibility.",
            )
        )
    elif solver_state == "INFEASIBLE" and not paths:
        paths.append(
            _path(
                "commitment_recovery",
                "Adjust delivery commitments that cannot all be met now",
                "Under the current hard due dates, shifts and capacity, the model has proven there is no complete feasible schedule.",
                [
                    {
                        "owner": "Agent",
                        "action": "Point out the conflicting orders, windows or resources and estimate overtime and due date change options.",
                    },
                    {
                        "owner": "Manager",
                        "action": "Choose an acceptable direction: overtime, subcontracting or a due date negotiation with the customer.",
                    },
                    {
                        "owner": "Shop floor",
                        "action": "Update the factory facts only after real capacity or customer commitments are confirmed.",
                    },
                ],
                "Find the current hard constraint conflicts and compare verifiable overtime, subcontracting and due date negotiation paths; reschedule for my approval once the conditions are confirmed.",
            )
        )
    return paths
