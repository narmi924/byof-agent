"""Versioned evidence and actual material/production records, without Solver-derived facts."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.domain.models import (
    ActualExecution,
    Assignment,
    Candidate,
    Resource,
    Snapshot,
    SolverPass,
)

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_FILE = ROOT / "data/development/skf-small.json"
CANDIDATE_FILE = ROOT / "tests/fixtures/contracts/p1-candidate.json"
BATCH = "SO-001-R001-B001"


def at(minute):
    return f"2026-09-14T00:{minute:02d}:00Z"


def legacy_snapshot():
    return json.loads(SNAPSHOT_FILE.read_bytes())


def legacy_candidate():
    return json.loads(CANDIDATE_FILE.read_bytes())


def actual_data():
    return {
        "operation_id": BATCH + "-OP20",
        "batch_id": BATCH,
        "route_version": "V1.6",
        "state": "BLOCKED",
        "actual_start": at(40),
        "actual_end": None,
        "resource_id": "ASMCELL-A",
        "worker_id": "W01",
        "completed_quantity": 0,
        "quality_state": "UNKNOWN",
        "remaining_minutes": None,
        "remaining_confirmed_by": None,
        "version": 3,
        "changeover_start": at(40),
        "remaining_setup_minutes": 0,
        "segments": [
            {
                "phase": "PRODUCTION",
                "source_event_id": "work-first",
                "start_at": at(40),
                "end_at": at(42),
            },
            {
                "phase": "PRODUCTION",
                "source_event_id": "work-resumed",
                "start_at": at(44),
                "end_at": at(45),
            },
        ],
        "consumed": [
            {
                "material_id": material,
                "quantity": 50,
                "unit": unit,
                "event_id": "consume-" + material,
            }
            for material, unit in (("IR-6202", "EA"), ("OR-6202", "EA"), ("BALLSET-6202", "SET"))
        ],
    }


def execution_snapshot():
    data = legacy_snapshot()
    data.update(
        schema_version="byof.snapshot/2",
        content_hash=None,
        snapshot_clock=at(45),
        active_plan_version="plan-1",
        active_plan_hash=legacy_candidate()["content_hash"],
    )
    data["source"].update(source_revision="4", observed_at=at(45), effective_at=at(45))
    kit = actual_data()
    kit.update(
        operation_id=BATCH + "-OP10",
        state="COMPLETED",
        actual_start=at(30),
        actual_end=at(40),
        changeover_start=at(30),
        resource_id="KIT-01",
        completed_quantity=50,
        consumed=[],
        remaining_minutes=0,
        remaining_confirmed_by="simulator:complete",
        segments=[
            {
                "phase": "PRODUCTION",
                "source_event_id": "kit-work",
                "start_at": at(30),
                "end_at": at(40),
            }
        ],
    )
    data["actuals"] = [kit, actual_data()]
    stock = {row["material_id"]: row for row in data["inventory"]}
    used = {row["material_id"]: row["quantity"] for row in data["actuals"][1]["consumed"]}
    data["reservations"] = []
    for bom in data["profile"]["bom"]:
        if bom["product_id"] != data["orders"][0]["product_id"]:
            continue
        material = bom["material_id"]
        remaining = bom["quantity_per_unit"] * 50 - used.get(material, 0)
        stock[material]["on_hand"] -= used.get(material, 0)
        stock[material]["reserved"] = remaining
        data["reservations"].append(
            {
                "reservation_id": "reservation-" + material,
                "batch_id": BATCH,
                "material_id": material,
                "quantity": remaining,
                "unit": stock[material]["unit"],
                "plan_version": "plan-1",
                "source_event_id": "kit-reserve-" + material,
                "created_at": at(30),
            }
        )
    return data


def test_actual_v1_snapshot_and_candidate_keep_their_original_bytes_and_hashes():
    fixtures = (
        (
            SNAPSHOT_FILE,
            Snapshot,
            "5386ddfc4db209c6721d5132ac42979ca9c6cfcd88662dca4b9066213fa8360a",
            "22633703eeea5e316d2bc8d1b206df01beea99751ebcce1432b150d33931516a",
        ),
        (
            CANDIDATE_FILE,
            Candidate,
            "6eac2c91c4851bdaea94c0db38bb9ce78d4cc004f5138aaea0dfd15b8e287e10",
            "828f65ef5870738ad0fd0dee3284df9472dde10ac22cb125f2c3cfd4f27ac664",
        ),
    )
    for path, model, file_hash, content_hash in fixtures:
        original = path.read_bytes()
        # Git may check JSON out using LF or CRLF; all other source bytes are fixed.
        assert hashlib.sha256(original.replace(b"\r\n", b"\n")).hexdigest() == file_hash
        decoded = model.model_validate_json(original)
        restored = model.model_validate_json(decoded.model_dump_json())
        assert restored == decoded and restored.content_hash == content_hash
        assert path.read_bytes() == original


def test_v1_snapshot_cannot_hide_new_execution_fields_outside_its_recorded_hash():
    base = legacy_snapshot()
    reservation = execution_snapshot()["reservations"][0]
    alterations = [
        {"active_plan_hash": "a" * 64},
        {"reservations": [reservation]},
        {
            "resources": [
                dict(
                    base["resources"][0],
                    last_operation_id=BATCH + "-OP10",
                    last_product_id="BRG-6202-2RS1",
                ),
                *base["resources"][1:],
            ]
        },
    ]
    for changes in alterations:
        with pytest.raises(ValidationError, match="VERSION_MISMATCH"):
            Snapshot.model_validate(dict(base, **changes))
    started = actual_data()
    started.update(actual_start=at(30), changeover_start=at(30), segments=[], consumed=[])
    with pytest.raises(ValidationError, match="VERSION_MISMATCH"):
        Snapshot.model_validate(dict(base, actuals=[started]))
    setup = dict(started, state="SETUP", actual_start=None, changeover_start=None)
    with pytest.raises(ValidationError, match="VERSION_MISMATCH"):
        Snapshot.model_validate(dict(base, actuals=[setup]))


def test_v1_candidate_cannot_hide_resumption_or_solver_search_evidence():
    base = legacy_candidate()
    changes = deepcopy(base)
    changes["assignments"][0].update(resume_at=at(35), resume_changeover_start=at(35))
    with pytest.raises(ValidationError, match="VERSION_MISMATCH"):
        Candidate.model_validate(changes)
    changes = deepcopy(base)
    changes.update(last_search_status="UNKNOWN")
    with pytest.raises(ValidationError):
        Candidate.model_validate(changes)
    changes = deepcopy(base)
    changes.update(last_search_status="OPTIMAL", solver_passes=[pass_data()])
    with pytest.raises(ValidationError, match="VERSION_MISMATCH"):
        Candidate.model_validate(changes)


def test_v2_hash_covers_setup_and_active_plan_state_and_remains_immutable():
    original = execution_snapshot()
    snapshot = Snapshot.model_validate(original)
    assert snapshot.schema_version == "byof.snapshot/2"
    assert Snapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    changed = deepcopy(original)
    changed["active_plan_hash"] = "b" * 64
    assert Snapshot.model_validate(changed).content_hash != snapshot.content_hash
    changed["content_hash"] = snapshot.content_hash
    with pytest.raises(ValidationError, match="HASH_MISMATCH"):
        Snapshot.model_validate(changed)
    with pytest.raises(ValidationError, match="frozen_instance"):
        snapshot.reservations[0].quantity = 999


def test_started_batch_keeps_original_consumption_remaining_reservations_and_external_stock():
    data = execution_snapshot()
    grease = next(i for i in data["inventory"] if i["material_id"] == "GREASE-G01")
    grease["reserved"] += 7
    snapshot = Snapshot.model_validate(data)
    assert sum(c.quantity for c in snapshot.actuals[1].consumed) == 150
    inner = next(i for i in snapshot.inventory if i.material_id == "IR-6202")
    assert (inner.on_hand, inner.reserved) == (1150, 0)
    grease = next(i for i in snapshot.inventory if i.material_id == "GREASE-G01")
    owned = sum(r.quantity for r in snapshot.reservations if r.material_id == grease.material_id)
    assert grease.reserved - owned == 7
    assert snapshot.actuals[1].remaining_minutes is None
    assert snapshot.actuals[1].quality_state == "UNKNOWN"


def test_missing_over_reserved_or_reconsumed_materials_are_rejected():
    data = execution_snapshot()
    cases = []
    missing = deepcopy(data)
    missing["reservations"].pop()
    cases.append(missing)
    extra = deepcopy(data)
    extra["reservations"][0]["quantity"] = 50
    cases.append(extra)
    unavailable = deepcopy(data)
    next(i for i in unavailable["inventory"] if i["material_id"] == "GREASE-G01")["reserved"] = 49
    cases.append(unavailable)
    consumed_again = deepcopy(data)
    consumed_again["actuals"][1]["consumed"][0]["quantity"] = 100
    cases.append(consumed_again)
    wrong_step = deepcopy(data)
    wrong_step["actuals"][0]["consumed"] = [deepcopy(data["actuals"][1]["consumed"][0])]
    cases.append(wrong_step)
    for invalid in cases:
        with pytest.raises(ValidationError, match="INVENTORY_CONFLICT"):
            Snapshot.model_validate(invalid)


def test_reservation_identity_batch_material_unit_and_creation_time_are_checked():
    base = execution_snapshot()
    changes = (
        {"batch_id": "absent-batch"},
        {"material_id": "absent-material"},
        {"material_id": "IR-6203"},
        {"unit": "GFU"},
        {"created_at": at(46)},
    )
    for change in changes:
        data = deepcopy(base)
        data["reservations"][0].update(change)
        with pytest.raises(ValidationError, match="INVALID_REFERENCE"):
            Snapshot.model_validate(data)
    duplicate = deepcopy(base)
    duplicate["reservations"].append(
        dict(duplicate["reservations"][0], reservation_id="another-id")
    )
    with pytest.raises(ValidationError, match="DUPLICATE_ID"):
        Snapshot.model_validate(duplicate)
    duplicate["reservations"][-1]["reservation_id"] = duplicate["reservations"][0]["reservation_id"]
    with pytest.raises(ValidationError, match="DUPLICATE_ID"):
        Snapshot.model_validate(duplicate)


def test_consumption_event_cannot_be_reused_by_a_different_operation():
    data = execution_snapshot()
    data["snapshot_clock"] = at(50)
    previous = data["actuals"][1]
    previous.update(
        state="COMPLETED",
        completed_quantity=50,
        actual_end=at(49),
        remaining_minutes=0,
        remaining_confirmed_by="simulator:complete",
    )
    next_actual = actual_data()
    next_actual.update(
        operation_id=BATCH + "-OP30",
        actual_start=at(49),
        changeover_start=at(49),
        resource_id="ASMCELL-B",
        segments=[],
        consumed=[
            {
                "material_id": "CAGE-6202",
                "unit": "SET",
                "quantity": 50,
                "event_id": previous["consumed"][0]["event_id"],
            }
        ],
    )
    data["actuals"].append(next_actual)
    cage = next(i for i in data["inventory"] if i["material_id"] == "CAGE-6202")
    cage.update(on_hand=cage["on_hand"] - 50, reserved=0)
    next(r for r in data["reservations"] if r["material_id"] == "CAGE-6202")["quantity"] = 0
    with pytest.raises(ValidationError, match="DUPLICATE_ID"):
        Snapshot.model_validate(data)


def test_setup_does_not_invent_production_start_or_consume_material():
    setup = actual_data()
    setup.update(
        state="SETUP",
        actual_start=None,
        consumed=[],
        remaining_setup_minutes=3,
        segments=[
            {
                "phase": "SETUP",
                "source_event_id": "setup-first",
                "start_at": at(40),
                "end_at": at(42),
            }
        ],
    )
    parsed = ActualExecution.model_validate(setup)
    assert parsed.actual_start is None and parsed.remaining_setup_minutes == 3
    for changes in (
        {"actual_start": at(41)},
        {"consumed": actual_data()["consumed"]},
        {"completed_quantity": 1},
    ):
        with pytest.raises(ValidationError):
            ActualExecution.model_validate(dict(setup, **changes))
    setup["segments"][0]["phase"] = "PRODUCTION"
    with pytest.raises(ValidationError, match="INVALID_TIME"):
        ActualExecution.model_validate(setup)


def test_interrupted_work_retains_gaps_and_unknown_remaining_work():
    actual = ActualExecution.model_validate(actual_data())
    elapsed_minutes = sum((s.end_at - s.start_at).total_seconds() / 60 for s in actual.segments)
    assert elapsed_minutes == 3
    assert (actual.segments[-1].end_at - actual.actual_start).total_seconds() / 60 == 5
    assert actual.remaining_minutes is None and actual.remaining_confirmed_by is None
    confirmed = dict(
        actual_data(), remaining_minutes=6, remaining_confirmed_by="supervisor:reply-1"
    )
    assert ActualExecution.model_validate(confirmed).remaining_minutes == 6
    with pytest.raises(ValidationError, match="CONFIRMATION_REQUIRED"):
        ActualExecution.model_validate(dict(confirmed, remaining_confirmed_by=None))


def test_execution_segment_reversal_overlap_and_repeated_source_event_are_rejected():
    base = actual_data()
    reversed_data = dict(base, segments=list(reversed(base["segments"])))
    overlap = deepcopy(base)
    overlap["segments"][1]["start_at"] = at(41)
    repeated = deepcopy(base)
    repeated["segments"][1]["source_event_id"] = repeated["segments"][0]["source_event_id"]
    for data in (reversed_data, overlap, repeated):
        with pytest.raises(ValidationError):
            ActualExecution.model_validate(data)


def test_different_operations_cannot_reuse_one_actual_segment_event():
    data = execution_snapshot()
    data["actuals"][1]["segments"][0]["source_event_id"] = data["actuals"][0]["segments"][0][
        "source_event_id"
    ]
    with pytest.raises(ValidationError, match="DUPLICATE_ID"):
        Snapshot.model_validate(data)


def test_actual_future_history_and_incomplete_completion_are_rejected():
    base = execution_snapshot()
    for actual_field, value in (("changeover_start", at(46)), ("actual_start", at(46))):
        data = deepcopy(base)
        data["actuals"][1][actual_field] = value
        with pytest.raises(ValidationError, match="INVALID_TIME"):
            Snapshot.model_validate(data)
    data = deepcopy(base)
    data["actuals"][1]["segments"][-1]["end_at"] = at(46)
    with pytest.raises(ValidationError, match="INVALID_TIME"):
        Snapshot.model_validate(data)
    data = deepcopy(base)
    data["actuals"][0]["completed_quantity"] = 49
    with pytest.raises(ValidationError, match="INVALID_INPUT"):
        Snapshot.model_validate(data)


def test_current_execution_version_and_setup_predecessor_require_complete_pairs():
    base = execution_snapshot()
    for changes in ({"active_plan_hash": None}, {"active_plan_version": None}):
        with pytest.raises(ValidationError, match="SOURCE_INCOMPLETE"):
            Snapshot.model_validate(dict(base, **changes))
    resource = legacy_snapshot()["resources"][0]
    for changes in ({"last_operation_id": "previous"}, {"last_product_id": "BRG-6202-2RS1"}):
        with pytest.raises(ValidationError, match="SOURCE_INCOMPLETE"):
            Resource.model_validate(dict(resource, **changes))
    parsed = Resource.model_validate(
        dict(resource, last_operation_id="previous", last_product_id="BRG-6202-2RS1")
    )
    assert parsed.last_operation_id == "previous"


def test_resumption_keeps_initial_history_and_has_ordered_future_setup_and_work():
    assignment = dict(
        legacy_candidate()["assignments"][1],
        changeover_start=at(35),
        end_at=at(59),
        resume_changeover_start=at(50),
        resume_at=at(55),
    )
    parsed = Assignment.model_validate(assignment)
    assert parsed.start_at.isoformat() == "2026-09-14T00:40:00+00:00"
    assert parsed.resume_at.isoformat() == "2026-09-14T00:55:00+00:00"
    for changes in (
        {"resume_at": None},
        {"resume_changeover_start": None},
        {"resume_changeover_start": at(56)},
        {"resume_at": at(59)},
        {"resume_changeover_start": at(35), "resume_at": at(39)},
    ):
        with pytest.raises(ValidationError):
            Assignment.model_validate(dict(assignment, **changes))


def pass_data():
    return {
        "objective_name": "weighted_tardiness",
        "native_status": "OPTIMAL",
        "has_solution": True,
        "objective_value": 0,
        "best_bound": 0.0,
        "wall_time_seconds": 0.1,
    }


def test_search_status_and_incumbent_presence_cannot_disagree():
    for status in ("OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE", "MODEL_INVALID"):
        has_solution = status in {"OPTIMAL", "FEASIBLE"}
        valid = dict(
            pass_data(),
            native_status=status,
            has_solution=has_solution,
            objective_value=0 if has_solution else None,
        )
        assert SolverPass.model_validate(valid).native_status == status
        for changes in (
            {"has_solution": not has_solution},
            {"objective_value": None if has_solution else 0},
        ):
            with pytest.raises(ValidationError, match="INVALID_INPUT"):
                SolverPass.model_validate(dict(valid, **changes))
    for changes in (
        {"best_bound": float("nan")},
        {"wall_time_seconds": -1},
        {"wall_time_seconds": float("inf")},
    ):
        with pytest.raises(ValidationError):
            SolverPass.model_validate(dict(pass_data(), **changes))


def test_later_unknown_search_retains_prior_incumbent_without_inventing_a_solution():
    data = legacy_candidate()
    unknown = dict(
        pass_data(),
        objective_name="makespan",
        native_status="UNKNOWN",
        has_solution=False,
        objective_value=None,
        best_bound=None,
    )
    data.update(
        schema_version="byof.candidate/2",
        content_hash=None,
        solver_passes=[pass_data(), unknown],
        last_search_status="UNKNOWN",
        proven_objective_levels=1,
    )
    retained = Candidate.model_validate(data)
    assert retained.native_status == "OPTIMAL" and retained.last_search_status == "UNKNOWN"
    assert len(retained.assignments) == 8 and retained.has_solution
    with pytest.raises(ValidationError, match="INVALID_INPUT"):
        Candidate.model_validate(dict(data, solver_passes=[unknown]))
    with pytest.raises(ValidationError, match="INVALID_INPUT"):
        Candidate.model_validate(dict(data, last_search_status="OPTIMAL"))


def test_candidate_cannot_claim_more_proven_levels_than_recorded_optimal_searches():
    data = legacy_candidate()
    data.update(
        schema_version="byof.candidate/2",
        content_hash=None,
        solver_passes=[pass_data()],
        last_search_status="OPTIMAL",
    )
    assert data["proven_objective_levels"] == 5
    with pytest.raises(ValidationError, match="INVALID_INPUT"):
        Candidate.model_validate(data)


def test_solver_search_cannot_claim_a_lower_bound_above_its_incumbent():
    for status in ("OPTIMAL", "FEASIBLE"):
        with pytest.raises(ValidationError, match="INVALID_INPUT"):
            SolverPass.model_validate(dict(pass_data(), native_status=status, best_bound=1.0))
