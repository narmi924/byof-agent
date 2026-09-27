"""Business contracts are checked independently of a solver or a model response."""

import csv
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from packages.domain.models import (
    ActualExecution,
    Approval,
    Candidate,
    CheckReport,
    ConnectorCapabilities,
    ConnectorMapping,
    Event,
    HumanTask,
    Inventory,
    Metric,
    Preference,
    Receipt,
    Release,
    Snapshot,
    ToolResult,
    batch_operations,
    canonical_hash,
    duration_minutes,
    minute_offset,
    topological_route,
)
from packages.domain.skf import BASELINE_MANIFEST, ROOT, load_skf_snapshot, verify_skf_baseline


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = load_skf_snapshot(development=True)

    def snapshot_data(self):
        return self.snapshot.model_dump(mode="json", exclude={"content_hash"})

    def candidate_data(self):
        snapshot = self.snapshot
        return {
            "candidate_id": "contract-candidate",
            "factory_id": snapshot.factory_id,
            "version": 1,
            "binding": {
                "snapshot_hash": snapshot.content_hash,
                "planning_revision": 1,
                "scope_version": 1,
                "profile_version": snapshot.profile.version,
                "policy_version": snapshot.profile.policy.policy_version,
                "objective_version": "delivery-v1",
                "baseline_plan_version": None,
            },
            "native_status": "FEASIBLE",
            "has_solution": True,
            "termination_reason": "TIME_LIMIT",
            "assignments": [
                {
                    "operation_id": "SO-001-R001-B001-OP10",
                    "resource_id": "KIT-01",
                    "worker_id": "W01",
                    "changeover_start": "2026-09-14T00:30:00Z",
                    "start_at": "2026-09-14T00:30:00Z",
                    "end_at": "2026-09-14T00:40:00Z",
                }
            ],
            "checker": {
                "checker_version": "contract-only",
                "snapshot_hash": snapshot.content_hash,
                "status": "NOT_RUN",
            },
            "effective_not_before": "2026-09-14T00:30:00Z",
            "accept_before": "2026-09-14T00:31:00Z",
        }

    def test_full_original_baseline_counts_and_identity_match_csv(self):
        snapshot = load_skf_snapshot()
        batches, operations = batch_operations(snapshot)
        self.assertEqual(
            (
                len(snapshot.orders),
                sum(o.quantity for o in snapshot.orders),
                len(batches),
                len(operations),
            ),
            (6, 5400, 108, 864),
        )
        for name, actual, key in (
            ("production_batches", batches, "batch_id"),
            ("operations", operations, "operation_id"),
        ):
            with (ROOT / "database/generated" / f"{name}.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                expected = {r[key] for r in csv.DictReader(handle)}
            self.assertEqual({getattr(row, key) for row in actual}, expected)
        self.assertEqual(load_skf_snapshot().content_hash, snapshot.content_hash)

    def test_development_case_uses_one_original_exact_lot(self):
        batches, operations = batch_operations(self.snapshot)
        self.assertEqual((len(self.snapshot.orders), len(batches), len(operations)), (1, 1, 8))
        self.assertEqual(batches[0].quantity, 50)
        product_id = batches[0].product_id
        route = topological_route(
            tuple(s for s in self.snapshot.profile.routes if s.product_id == product_id)
        )
        self.assertEqual(
            [s.operation_code for s in route],
            ["OP10", "OP20", "OP30", "OP60", "OP40", "OP50", "OP70", "OP80"],
        )
        self.assertEqual(sum(duration_minutes(s, 50) for s in route), 66)
        self.assertEqual(topological_route(tuple(reversed(route))), route)
        seals = [
            b
            for b in self.snapshot.profile.bom
            if b.product_id == product_id and b.material_id.startswith("SEAL-")
        ]
        self.assertEqual(sum(b.quantity_per_unit * 50 for b in seals), 100)
        self.assertTrue(all(s.quality_threshold is None for s in route))
        self.assertIsNone(self.snapshot.profile.policy.overtime_cost_per_minute)

    def test_saved_shared_case_and_rejection_are_reproducible(self):
        fixture = ROOT / "data/development/skf-small.json"
        self.assertEqual(Snapshot.model_validate_json(fixture.read_bytes()), self.snapshot)
        bad = json.loads((ROOT / "data/development/reject-nonmultiple.json").read_text())
        data = self.snapshot_data()
        data["orders"][0]["quantity"] = bad["quantity"]
        with self.assertRaisesRegex(ValidationError, bad["expected_error"]):
            Snapshot.model_validate(data)

    def test_quantity_types_no_coercion_rounding_or_truncation(self):
        for quantity in (True, False, "50", 50.0, 50.5, 0, -50, 51):
            with self.subTest(quantity=quantity):
                data = self.snapshot_data()
                data["orders"][0]["quantity"] = quantity
                with self.assertRaises(ValidationError):
                    Snapshot.model_validate(data)
        data = self.snapshot_data()
        data["orders"][0]["quantity"] = 100
        changed = Snapshot.model_validate(data)
        self.assertEqual(sum(b.quantity for b in batch_operations(changed)[0]), 100)
        self.assertNotEqual(changed.content_hash, self.snapshot.content_hash)

    def test_unknown_unit_missing_duration_cycle_and_missing_skill_rejected(self):
        changes = [
            ("unit", "UNKNOWN_UNIT"),
            ("duration", "Field required"),
            ("cycle", "CYCLIC_ROUTE"),
            ("skill", "INVALID_REFERENCE"),
            ("capability", "UNSUPPORTED_CAPABILITY"),
            ("capacity", "Input should be less than or equal to 1"),
        ]
        for change, code in changes:
            with self.subTest(change=change):
                data = self.snapshot_data()
                if change == "unit":
                    data["profile"]["materials"][0]["unit"] = "kg"
                elif change == "duration":
                    del data["profile"]["routes"][0]["cycle_sec_per_unit"]
                elif change == "cycle":
                    data["profile"]["routes"][0]["predecessors"] = [
                        data["profile"]["routes"][7]["step_id"]
                    ]
                elif change == "skill":
                    data["profile"]["routes"][0]["skill"] = "MISSING"
                elif change == "capability":
                    data["profile"]["required_capabilities"].append("execute_uploaded_python")
                else:
                    data["resources"][0]["capacity"] = 2
                with self.assertRaisesRegex(ValidationError, code):
                    Snapshot.model_validate(data)

    def test_original_baseline_change_is_different_from_valid_new_scenario(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in json.loads(BASELINE_MANIFEST.read_text()):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            self.assertEqual(verify_skf_baseline(root), verify_skf_baseline())
            target = root / "database/seed/sales_orders.csv"
            target.write_text(target.read_text().replace(",1000,", ",1050,", 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Original SKF baseline differs"):
                verify_skf_baseline(root)
        data = self.snapshot_data()
        data["orders"][0]["quantity"] = 100
        self.assertEqual(Snapshot.model_validate(data).orders[0].quantity, 100)

    def test_snapshot_is_nested_immutable_and_hash_detects_tampering(self):
        with self.assertRaises(ValidationError):
            self.snapshot.orders[0].quantity = 100
        with self.assertRaises(TypeError):
            self.snapshot.orders[0] = self.snapshot.orders[0]
        data = self.snapshot.model_dump(mode="json")
        data["orders"][0]["quantity"] = 100
        with self.assertRaisesRegex(ValidationError, "HASH_MISMATCH"):
            Snapshot.model_validate(data)
        self.assertEqual(canonical_hash({"b": 1, "a": 2}), canonical_hash({"a": 2, "b": 1}))
        bypassed = self.snapshot.model_copy(
            update={"orders": (self.snapshot.orders[0].model_copy(update={"quantity": True}),)}
        )
        with self.assertRaises(ValidationError):
            Snapshot.model_validate(bypassed)

    def test_timezone_normalized_naive_and_numeric_rejected(self):
        data = self.snapshot.model_dump(mode="json")
        data["snapshot_clock"] = "2026-09-14T08:30:00+08:00"
        self.assertEqual(Snapshot.model_validate(data).content_hash, self.snapshot.content_hash)
        for value in ("2026-09-14T08:30:00", 12345, True):
            data = self.snapshot_data()
            data["snapshot_clock"] = value
            with self.assertRaises(ValidationError):
                Snapshot.model_validate(data)
        origin = datetime(2026, 9, 14, tzinfo=timezone.utc)
        self.assertEqual(minute_offset(origin, origin + timedelta(seconds=61), round_up=True), 2)
        self.assertEqual(minute_offset(origin, origin + timedelta(seconds=61), round_up=False), 1)
        self.assertEqual(minute_offset(origin, origin + timedelta(days=1), round_up=True), 1440)

    def test_inventory_includes_reserved_and_receipts_are_not_double_counted(self):
        row = Inventory(material_id="material", unit="EA", on_hand=10, reserved=4)
        self.assertEqual(row.on_hand - row.reserved, 6)
        with self.assertRaisesRegex(ValidationError, "INVENTORY_CONFLICT"):
            Inventory(material_id="material", unit="EA", on_hand=10, reserved=11)
        data = {
            "receipt_id": "receipt",
            "material_id": "material",
            "unit": "EA",
            "quantity": 10,
            "eta": "2026-09-14T00:30:00Z",
            "status": "RECEIVED",
        }
        with self.assertRaises(ValidationError):
            Receipt.model_validate(data)
        data["received_at"] = data["eta"]
        self.assertEqual(Receipt.model_validate(data).status, "RECEIVED")
        snapshot = self.snapshot_data()
        snapshot["receipts"].append(snapshot["receipts"][0])
        with self.assertRaisesRegex(ValidationError, "DUPLICATE_ID"):
            Snapshot.model_validate(snapshot)

    def test_actual_work_remaining_requires_confirmation_and_preserves_consumption(self):
        data = {
            "operation_id": "op",
            "batch_id": "batch",
            "route_version": "v1",
            "state": "BLOCKED",
            "actual_start": "2026-09-14T00:30:00Z",
            "resource_id": "r",
            "worker_id": "w",
            "completed_quantity": 20,
            "version": 1,
            "consumed": [
                {"material_id": "m", "quantity": 50, "unit": "EA", "event_id": "consume-1"}
            ],
        }
        actual = ActualExecution.model_validate(data)
        self.assertIsNone(actual.remaining_minutes)
        self.assertEqual(actual.consumed[0].quantity, 50)
        data["remaining_minutes"] = 5
        with self.assertRaisesRegex(ValidationError, "CONFIRMATION_REQUIRED"):
            ActualExecution.model_validate(data)
        data["remaining_confirmed_by"] = "source-report-1"
        self.assertEqual(ActualExecution.model_validate(data).remaining_minutes, 5)

    def test_candidate_native_status_and_content_hash_preserved(self):
        candidate = Candidate.model_validate(self.candidate_data())
        self.assertEqual(
            (
                candidate.native_status,
                candidate.termination_reason,
                candidate.proven_objective_levels,
            ),
            ("FEASIBLE", "TIME_LIMIT", 0),
        )
        changed = candidate.model_dump(mode="json")
        changed["assignments"][0]["end_at"] = "2026-09-14T00:41:00Z"
        with self.assertRaisesRegex(ValidationError, "HASH_MISMATCH"):
            Candidate.model_validate(changed)
        for status in ("UNKNOWN", "INFEASIBLE", "MODEL_INVALID"):
            data = self.candidate_data()
            data["native_status"] = status
            with self.assertRaises(ValidationError):
                Candidate.model_validate(data)
            data.update(has_solution=False, assignments=[])
            self.assertFalse(Candidate.model_validate(data).has_solution)

    def test_actual_execution_cannot_reference_orphan_work_or_exceed_its_batch(self):
        data = self.snapshot_data()
        actual = {
            "operation_id": "SO-001-R001-B001-OP10",
            "batch_id": "SO-001-R001-B001",
            "route_version": "V1.6",
            "state": "BLOCKED",
            "actual_start": data["snapshot_clock"],
            "resource_id": "KIT-01",
            "worker_id": "W01",
            "completed_quantity": 20,
            "version": 1,
        }
        data["actuals"] = [actual]
        self.assertEqual(Snapshot.model_validate(data).actuals[0].completed_quantity, 20)
        for field, value in (
            ("operation_id", "orphan"),
            ("batch_id", "orphan"),
            ("route_version", "changed-route"),
            ("completed_quantity", 51),
        ):
            with self.subTest(field=field):
                changed = self.snapshot_data()
                changed["actuals"] = [{**actual, field: value}]
                with self.assertRaises(ValidationError):
                    Snapshot.model_validate(changed)

    def test_unknown_metrics_do_not_become_zero_or_fake_pass(self):
        metric = Metric(
            name="overtime_cost", value=None, unit="currency", unknown_reason="Missing rate"
        )
        self.assertIsNone(metric.value)
        with self.assertRaises(ValidationError):
            Metric(name="overtime_cost", value=None, unit="currency")
        with self.assertRaises(ValidationError):
            CheckReport(
                checker_version="v1",
                snapshot_hash=self.snapshot.content_hash,
                status="PASS",
                issues=({"code": "CONFLICT", "message": "Resource conflict"},),
            )

    def test_approval_real_clock_and_local_release_do_not_imply_source_activation(self):
        candidate = Candidate.model_validate(self.candidate_data())
        data = {
            "approval_id": "approve-1",
            "factory_id": self.snapshot.factory_id,
            "candidate_hash": candidate.content_hash,
            "binding": candidate.binding.model_dump(mode="json"),
            "approver_id": "planner-1",
            "approver_role": "planner",
            "action_scope": "publish_plan",
            "decision": "APPROVED",
            "decided_at": "2026-09-16T00:00:00Z",
            "expires_at": "2026-09-16T00:05:00Z",
        }
        self.assertEqual(Approval.model_validate(data).clock, "real")
        data["clock"] = "business"
        with self.assertRaises(ValidationError):
            Approval.model_validate(data)
        release = {
            "release_id": "release-1",
            "factory_id": self.snapshot.factory_id,
            "operation_id": "stable-op-1",
            "candidate_hash": candidate.content_hash,
            "payload_hash": candidate.content_hash,
            "approval_ids": ["approve-1"],
            "expected_source_revision": "1",
            "expected_active_plan_version": None,
            "local_state": "LOCAL_COMMITTED",
            "source_state": "PENDING_SOURCE",
            "committed_at": "2026-09-16T00:00:00Z",
        }
        self.assertEqual(Release.model_validate(release).execution_state, "NOT_STARTED")
        release["source_state"] = "ACTIVE"
        with self.assertRaises(ValidationError):
            Release.model_validate(release)

    def test_human_task_delivery_and_handling_are_independent(self):
        task = HumanTask(
            human_task_id="task-1",
            factory_id="factory",
            case_id="case",
            version=1,
            owner_id="user",
            owner_role="maintenance_owner",
            question="Expected recovery time?",
            due_at="2026-09-16T00:00:00Z",
            send_state="PROVIDER_ACCEPTED",
        )
        self.assertEqual((task.delivery_state, task.handling_state), ("UNAVAILABLE", "OPEN"))
        self.assertFalse(task.real_delivery_permitted)

    def test_model_fields_cannot_confirm_events_or_activate_profile(self):
        event = {
            "event_id": "e1",
            "factory_id": "f1",
            "run_id": "r1",
            "source_event_id": "e1",
            "source_revision": "1",
            "entity_type": "resource",
            "entity_id": "r1",
            "entity_version": 1,
            "event_type": "resource.down",
            "occurred_at": "2026-09-14T00:30:00Z",
            "observed_at": "2026-09-16T00:30:00Z",
            "effective_at": "2026-09-14T00:30:00Z",
            "changes": [{"field": "status", "before": "AVAILABLE", "after": "DOWN"}],
            "confirmed": True,
        }
        with self.assertRaises(ValidationError):
            Event.model_validate(event)
        del event["confirmed"]
        self.assertEqual(Event.model_validate(event).verification_state, "PENDING_SERVER_CHECK")
        data = self.snapshot_data()
        data["profile"]["activation_state"] = "ACTIVE"
        with self.assertRaisesRegex(ValidationError, "CONFIRMATION_REQUIRED"):
            Snapshot.model_validate(data)

    def test_preference_cannot_change_hard_rules_and_tool_scope_is_enforced(self):
        preference = {
            "preference_id": "pref",
            "factory_id": "f",
            "version": 1,
            "scope_type": "CASE",
            "scope_id": "case",
            "base_policy_version": "policy",
            "selection": "delivery_first",
            "objective_order": [
                "weighted_tardiness",
                "incremental_overtime_metric",
                "changed_operations",
                "total_start_shift",
                "makespan",
            ],
        }
        self.assertIsNone(Preference.model_validate(preference).currency_rate)
        preference["ignore_hard_deadlines"] = True
        with self.assertRaises(ValidationError):
            Preference.model_validate(preference)
        with self.assertRaisesRegex(ValidationError, "INVALID_REFERENCE"):
            ToolResult(
                operation_id="read-1",
                factory_id="other",
                status="OK",
                summary="Read facts",
                snapshot=self.snapshot,
            )
        data = self.snapshot_data()
        data["orders"][0]["version"] = 2
        changed = Snapshot.model_validate(data)
        self.assertEqual(batch_operations(changed)[0][0].batch_id, "SO-001-R001-B001")

    def test_connector_mapping_uses_registry_ids_and_explicit_inventory_semantics(self):
        data = {
            "mapping_id": "map-1",
            "version": 1,
            "factory_id": "f1",
            "source_id": "source-1",
            "origin_id": "configured-factory",
            "endpoint_bindings": [{"operation": "read_snapshot", "endpoint_id": "snapshot-v1"}],
            "fields": [{"entity": "order", "source_field": "Qty", "target_field": "quantity"}],
            "units": [{"source_unit": "pieces", "target_unit": "EA"}],
            "statuses": [
                {"entity": "resource", "source_status": "ready", "target_status": "AVAILABLE"}
            ],
            "inventory_reserved_semantics": "included_in_on_hand",
        }
        self.assertEqual(ConnectorMapping.model_validate(data).units[0].target_unit, "EA")
        for field, value in (
            ("origin_id", "https://arbitrary.invalid"),
            ("inventory_reserved_semantics", "unknown"),
        ):
            bad = dict(data)
            bad[field] = value
            with self.assertRaises(ValidationError):
                ConnectorMapping.model_validate(bad)
        data["statuses"][0]["target_status"] = "MAYBE_READY"
        with self.assertRaises(ValidationError):
            ConnectorMapping.model_validate(data)
        data["statuses"][0]["target_status"] = "UNKNOWN"
        data["units"][0]["target_unit"] = "kg"
        with self.assertRaises(ValidationError):
            ConnectorMapping.model_validate(data)

    def test_connector_cannot_claim_conditional_acceptance_without_write_capability(self):
        data = {
            "read_snapshot": True,
            "read_changes": False,
            "query_detail": False,
            "accept_plan": False,
            "query_action": False,
            "idempotency": False,
            "conditional_acceptance": True,
            "snapshot_consistency": "ATOMIC_SNAPSHOT",
        }
        with self.assertRaises(ValidationError):
            ConnectorCapabilities.model_validate(data)
        data["conditional_acceptance"] = False
        self.assertFalse(ConnectorCapabilities.model_validate(data).accept_plan)


if __name__ == "__main__":
    unittest.main()
