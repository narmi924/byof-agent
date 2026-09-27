"""Durable follow-up for explicitly selected recovery routes, without opening new cases."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.agent.assistant_store import AssistantAction
from packages.agent.cases_store import CaseOperation, CaseRecord, add_input
from packages.agent.human_tasks import HumanTaskRecord
from packages.agent.recovery_paths import recovery_paths
from packages.domain.models import Snapshot, canonical_hash
from packages.domain.production_facts import material_shortfalls
from packages.persistence import Membership, User
from packages.planning.service import active_baseline

TERMINAL = {"RESOLVED", "CANCELLED", "HANDED_OFF"}


def followup_key(snapshot: Snapshot, kind: str) -> str:
    """Normal clock ticks and completed operations do not generate procurement follow-ups."""
    orders = [
        {
            "id": o.order_id,
            "quantity": o.quantity,
            "due": o.due_at.isoformat(),
            "hard": o.hard_deadline,
        }
        for o in snapshot.orders
    ]
    capacity = {
        "resources": [
            r.model_dump(mode="json", exclude={"version", "last_operation_id", "last_product_id"})
            for r in snapshot.resources
        ],
        "workers": [w.model_dump(mode="json", exclude={"version"}) for w in snapshot.workers],
    }
    if kind in {"material_supply", "material_customer_terms"}:
        facts: object = {
            "orders": orders,
            "gaps": [
                {"material": g["material_id"], "missing": g["minimum_shortfall"]}
                for g in material_shortfalls(snapshot)
            ],
            "receipts": [
                r.model_dump(mode="json", exclude={"version", "received_at"})
                for r in snapshot.receipts
            ],
        }
    elif kind in {"equipment_recovery", "workforce_recovery", "capacity_window_recovery"}:
        facts = {
            "orders": orders,
            **capacity,
        }
    else:
        facts = {
            "orders": orders,
            **capacity,
            "gaps": [
                {"material": g["material_id"], "missing": g["minimum_shortfall"]}
                for g in material_shortfalls(snapshot)
            ],
            "receipts": [
                r.model_dump(mode="json", exclude={"version", "received_at"})
                for r in snapshot.receipts
            ],
            "blocked": [
                a.model_dump(mode="json")
                for a in snapshot.actuals
                if a.state == "BLOCKED" or a.quality_state == "FAILED"
            ],
            "batches": [
                b.model_dump(mode="json")
                for b in snapshot.production_batches or ()
                if b.purpose == "SCRAP"
            ],
        }
    return canonical_hash({"kind": kind, "facts": facts})


def selected_rows(db: Session, snapshot: Snapshot) -> list[AssistantAction]:
    return list(
        db.scalars(
            select(AssistantAction)
            .where(
                AssistantAction.factory_id == snapshot.factory_id,
                AssistantAction.run_id == snapshot.run_id,
                AssistantAction.kind == "recover",
                AssistantAction.state == "DONE",
            )
            .order_by(AssistantAction.created_at.desc())
        )
    )


def reconcile_recoveries(db: Session, snapshot: Snapshot) -> int:
    paths = {p["kind"]: p for p in recovery_paths(snapshot, active_baseline(db, snapshot))}
    count = 0
    for row in selected_rows(db, snapshot):
        result = row.result or {}
        path = result.get("path")
        if not isinstance(path, dict) or result.get("cancelled_at"):
            continue
        case = db.get(CaseRecord, row.payload.get("case_id"), with_for_update=True)
        if case is None or case.run_id != snapshot.run_id or case.state in TERMINAL:
            continue
        user = db.get(User, case.owner_id)
        if (
            not user
            or not user.active
            or not db.get(Membership, (case.owner_id, snapshot.factory_id, "manager"))
        ):
            continue
        key = followup_key(snapshot, path["kind"])
        prior = result.get("followup_key")
        if prior == key:
            continue
        # Existing selected routes are adopted once after upgrade. The preserved
        # choice authorizes a recheck, not a source write or plan approval.
        current = paths.get(path["kind"])
        row.result = {
            **result,
            "followup_key": key,
            "current_path": current,
            "last_source_change_at": datetime.now(UTC).isoformat(),
        }
        add_input(
            db,
            case,
            f"recovery:{row.action_id}:{snapshot.source.source_revision}",
            "SOURCE",
            {
                "recovery_action_id": row.action_id,
                "kind": path["kind"],
                "remaining_condition": current,
                "message": "An authorized recovery item received a related shop floor change. Check the remaining conditions; when they are met, keep solving and ask the manager for approval.",
            },
        )
        count += 1
    return count


def recovery_view(db: Session, row: AssistantAction, snapshot: Snapshot, paths: dict) -> dict:
    result = row.result or {}
    original = result["path"]
    current = paths.get(original["kind"])
    case = db.get(CaseRecord, row.payload.get("case_id"))
    state = "AWAITING_SOURCE" if current else "RECHECKING"
    if result.get("cancelled_at"):
        state = "CANCELLED"
    elif case and case.state in TERMINAL:
        state = "RESOLVED" if case.state == "RESOLVED" else "CANCELLED"
    elif case and case.error_code:
        state = "NEEDS_ATTENTION"
    elif case and case.state in {"OPEN", "INVESTIGATING", "PLANNING"}:
        state = "RECHECKING"
    elif (
        case
        and not current
        and db.scalar(
            select(HumanTaskRecord.task_id)
            .join(CaseOperation, CaseOperation.operation_id == HumanTaskRecord.operation_id)
            .where(
                HumanTaskRecord.case_id == case.case_id,
                HumanTaskRecord.state.in_(("OPEN", "ESCALATED")),
                CaseOperation.action == "request_approval",
            )
            .limit(1)
        )
    ):
        state = "AWAITING_APPROVAL"
    next_action = {
        "AWAITING_SOURCE": "The shop floor verifies and records the remaining conditions; it continues automatically after the source changes.",
        "RECHECKING": "The Agent is verifying the shop floor change; this does not mean recovery or approval is done.",
        "AWAITING_APPROVAL": "The plan was submitted to the manager and waits for an explicit approval.",
        "NEEDS_ATTENTION": "Automatic follow-up hit a problem; the manager should open the original conversation to see why and retry.",
        "RESOLVED": "The original item is done; this record is kept for tracing.",
        "CANCELLED": "Follow-up of this item stopped.",
    }[state]
    return {
        "action_id": row.action_id,
        "case_id": row.payload.get("case_id"),
        "created_at": row.created_at,
        "path": {**(current or original), "path_id": "recovery-" + original["kind"]},
        "state": state,
        "condition_remaining": current is not None,
        "last_source_change_at": result.get("last_source_change_at"),
        "next_action": next_action,
    }
