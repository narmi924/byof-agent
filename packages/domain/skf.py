"""Read-only mapping of the pinned SKF CSV baseline into shared contracts."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from packages.domain.models import (
    KNOWN_CAPABILITIES,
    ErrorCode,
    Snapshot,
    canonical_hash,
    reject,
)

ROOT = Path(__file__).resolve().parents[2]
BASELINE_MANIFEST = ROOT / "data/development/skf-baseline-hashes.json"


def baseline_file_digest(path: Path) -> str:
    # Git normalizes CSV/SQL/JSON line endings; the domain spec retains its original byte hash.
    raw = (
        path.read_bytes()
        if path.suffix == ".md"
        else path.read_text(encoding="utf-8").encode("utf-8")
    )
    return hashlib.sha256(raw).hexdigest()


def verify_skf_baseline(root: Path = ROOT) -> str:
    """Pin original inputs; new scenarios use Snapshot validation without this baseline check."""
    expected = json.loads(BASELINE_MANIFEST.read_text(encoding="utf-8"))
    for relative, digest in expected.items():
        path = root / relative
        if not path.is_file() or baseline_file_digest(path) != digest:
            reject(ErrorCode.BASELINE_CHANGED, f"Original SKF baseline differs: {relative}")
    return canonical_hash(expected)


def _rows(root: Path, name: str) -> list[dict[str, str]]:
    with (root / "database/seed" / f"{name}.csv").open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_skf_snapshot(*, development: bool = False, root: Path = ROOT) -> Snapshot:
    baseline_digest = verify_skf_baseline(root)
    settings = _rows(root, "planning_settings")[0]
    source = json.loads((root / "database/source_manifest.json").read_text(encoding="utf-8"))
    products = _rows(root, "products")
    route_rows = _rows(root, "routing_steps")
    routes = []
    for product in products:
        ordered = sorted(
            (s for s in route_rows if s["product_id"] == product["product_id"]),
            key=lambda s: int(s["sequence_no"]),
        )
        previous = None
        for step in ordered:
            routes.append(
                {
                    "step_id": step["routing_step_id"],
                    "product_id": step["product_id"],
                    "route_version": step["routing_version"],
                    "operation_code": step["op_code"],
                    "name": step["operation_name"],
                    "predecessors": [previous] if previous else [],
                    "setup_min": int(step["setup_min"]),
                    "cycle_sec_per_unit": int(step["cycle_sec_per_unit"]),
                    "resource_type": step["required_resource_type"],
                    "skill": step["required_skill"],
                    "quality_gate": step["op_code"] in ("OP60", "OP70"),
                    "quality_threshold": None,
                }
            )
            previous = step["routing_step_id"]
    step_ids = {(s["product_id"], s["op_code"]): s["routing_step_id"] for s in route_rows}
    materials = [
        {"material_id": m["material_id"], "name": m["material_name"], "unit": m["unit"]}
        for m in _rows(root, "materials")
    ]
    units = {m["material_id"]: m["unit"] for m in materials}
    calendar = [
        {
            "start_at": c["start_at"],
            "end_at": c["end_at"],
            "kind": "OVERTIME" if c["period_type"] == "OVERTIME_WINDOW" else "NORMAL",
        }
        for c in _rows(root, "calendar_periods")
        if c["period_type"] != "BREAK"
    ]
    capabilities = _rows(root, "resource_operations")
    skills = _rows(root, "worker_skills")
    weights = {p["priority"]: int(p["tardiness_weight"]) for p in _rows(root, "priority_levels")}
    order_rows = _rows(root, "sales_orders")[:1] if development else _rows(root, "sales_orders")
    product_lots = {p["product_id"]: int(p["batch_size"]) for p in products}
    factory_id = "skf-development" if development else "skf-reference"
    data = {
        "snapshot_id": "skf-development-v1" if development else "skf-original-v1.6",
        "factory_id": factory_id,
        "run_id": "development-initial" if development else "baseline-initial",
        "snapshot_clock": settings["snapshot_clock"],
        "horizon": {"start_at": settings["horizon_start"], "end_at": settings["horizon_end"]},
        "source": {
            "source_system": "skf-static-csv",
            "source_revision": baseline_digest,
            "cursor": None,
            "observed_at": settings["snapshot_clock"],
            "effective_at": settings["snapshot_clock"],
            "complete": True,
            "consistency": "ATOMIC_SNAPSHOT",
            "freshness": "CURRENT",
            "ownership": "simulator_fact",
            "evidence_digest": baseline_digest,
        },
        "planning_revision": 1,
        "scope_version": 1,
        "active_plan_version": None,
        "profile": {
            "factory_id": factory_id,
            "profile_id": "skf-assembly-reference",
            "version": "V1.6",
            "timezone": settings["display_timezone"],
            "activation_state": "DRAFT",
            "evidence_mode": "public_reference_plus_synthetic_operations",
            "source_digest": source["source_sha256"],
            "required_capabilities": sorted(KNOWN_CAPABILITIES),
            "products": [
                {
                    "product_id": p["product_id"],
                    "name": p["product_name"],
                    "batch_size": int(p["batch_size"]),
                    "route_version": "V1.6",
                }
                for p in products
            ],
            "materials": materials,
            "routes": routes,
            "bom": [
                {
                    "product_id": b["product_id"],
                    "material_id": b["material_id"],
                    "quantity_per_unit": int(b["qty_per_unit"]),
                    "consume_step_id": step_ids[b["product_id"], b["consume_op_code"]],
                }
                for b in _rows(root, "bom_items")
            ],
            "policy": {
                "policy_version": "skf-v1.6-default",
                "first_changeover_min": int(settings["first_changeover_min"]),
                "same_product_changeover_min": int(settings["same_sku_changeover_min"]),
                "different_product_changeover_min": int(settings["different_sku_changeover_min"]),
                "freeze_window_min": int(settings["freeze_window_min"]),
            },
        },
        "orders": [
            {
                "order_id": o["order_id"],
                "product_id": o["product_id"],
                "quantity": product_lots[o["product_id"]] if development else int(o["quantity"]),
                "due_at": o["due_at"],
                "priority_weight": weights[o["priority"]],
                "hard_deadline": o["hard_deadline"] == "1",
                "version": int(o["version"]),
            }
            for o in order_rows
        ],
        "inventory": [
            {
                "material_id": i["material_id"],
                "unit": units[i["material_id"]],
                "on_hand": int(i["on_hand"]),
                "reserved": int(i["reserved"]),
            }
            for i in _rows(root, "inventory")
        ],
        "receipts": [
            {
                "receipt_id": r["inbound_id"],
                "material_id": r["material_id"],
                "unit": units[r["material_id"]],
                "quantity": int(r["quantity"]),
                "eta": r["eta"],
                "status": r["status"],
            }
            for r in _rows(root, "material_inbounds")
        ],
        "resources": [
            {
                "resource_id": r["resource_id"],
                "resource_type": r["resource_type"],
                "capacity": int(r["capacity"]),
                "status": r["status"],
                "calendar": calendar,
                "operation_codes": [
                    c["op_code"] for c in capabilities if c["resource_id"] == r["resource_id"]
                ],
            }
            for r in _rows(root, "resources")
        ],
        "workers": [
            {
                "worker_id": w["worker_id"],
                "status": w["status"],
                "overtime_available": w["overtime_available"] == "1",
                "calendar": calendar,
                "skills": [s["skill"] for s in skills if s["worker_id"] == w["worker_id"]],
            }
            for w in _rows(root, "workers")
        ],
    }
    return Snapshot.model_validate(data)
