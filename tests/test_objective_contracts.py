"""Objective order, explicit bounds and provenance never alter original factory facts."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.domain.execution import PlanSubmission
from packages.domain.models import Candidate, Snapshot, canonical_hash
from packages.domain.objectives import (
    METRIC_NAMES,
    METRIC_UNITS,
    EffectiveObjective,
    ObjectiveDefinition,
    ObjectiveSource,
)

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = (
    "weighted_tardiness",
    "incremental_overtime_metric",
    "changed_operations",
    "total_start_shift",
    "makespan",
)
STABILITY = (
    "changed_operations",
    "total_start_shift",
    "weighted_tardiness",
    "incremental_overtime_metric",
    "makespan",
)
OVERTIME = (
    "incremental_overtime_metric",
    "weighted_tardiness",
    "changed_operations",
    "total_start_shift",
    "makespan",
)


@pytest.fixture
def snapshot():
    return Snapshot.model_validate_json((ROOT / "data/development/skf-small.json").read_bytes())


def source_data(snapshot, **updates):
    return {
        "preference_id": "preference-1",
        "version": 1,
        "scope_type": "FACTORY",
        "scope_id": snapshot.factory_id,
        "confirmed_by": "planner-1",
        "confirmed_at": "2026-09-17T08:00:00+08:00",
        **updates,
    }


def objective_data(snapshot, **updates):
    return {
        "factory_id": snapshot.factory_id,
        "profile_version": snapshot.profile.version,
        "policy_version": snapshot.profile.policy.policy_version,
        "definition": {"selection": "delivery_first"},
        "sources": [source_data(snapshot)],
        **updates,
    }


@pytest.mark.parametrize(
    ("inputs", "order", "bounds"),
    [
        ({}, DELIVERY, {}),
        (
            {
                "selection": "stability_first",
                "max_weighted_tardiness": 30,
                "max_incremental_overtime_minutes": 0,
            },
            STABILITY,
            {"weighted_tardiness": 30, "incremental_overtime_metric": 0},
        ),
        (
            {"selection": "overtime_first", "max_weighted_tardiness": 0},
            OVERTIME,
            {"weighted_tardiness": 0},
        ),
    ],
)
def test_named_preferences_have_complete_prescribed_orders_and_auditable_bounds(
    inputs, order, bounds
):
    definition = ObjectiveDefinition(**inputs)
    assert definition.order == order
    assert definition.bounds == bounds
    assert set(definition.order) == set(METRIC_NAMES)
    assert METRIC_NAMES == DELIVERY
    assert dict(METRIC_UNITS) == {
        "weighted_tardiness": "minutes",
        "incremental_overtime_metric": "minutes",
        "changed_operations": "operations",
        "total_start_shift": "minutes",
        "makespan": "minutes",
    }


@pytest.mark.parametrize("selection", ["delivery_first", "stability_first", "overtime_first"])
def test_named_selection_cannot_claim_a_different_order(selection):
    with pytest.raises(ValidationError, match="defined objective order"):
        ObjectiveDefinition(
            selection=selection,
            objective_order=tuple(reversed(DELIVERY)),
            max_weighted_tardiness=60,
            max_incremental_overtime_minutes=60,
        )


@pytest.mark.parametrize(
    "inputs",
    [
        {"selection": "stability_first"},
        {"selection": "stability_first", "max_weighted_tardiness": 0},
        {"selection": "stability_first", "max_incremental_overtime_minutes": 0},
        {"selection": "overtime_first"},
        {"selection": "custom", "objective_order": OVERTIME},
        {"selection": "custom", "objective_order": STABILITY, "max_weighted_tardiness": 0},
        {
            "selection": "custom",
            "objective_order": (DELIVERY[0], DELIVERY[2], DELIVERY[1], *DELIVERY[3:]),
        },
    ],
)
def test_lower_delivery_or_overtime_priority_requires_specific_confirmation_bounds(inputs):
    with pytest.raises(ValidationError) as caught:
        ObjectiveDefinition(**inputs)
    assert caught.value.errors()[0]["type"] == "CONFIRMATION_REQUIRED"


def test_custom_order_must_be_explicit():
    with pytest.raises(ValidationError) as caught:
        ObjectiveDefinition(selection="custom")
    assert caught.value.errors()[0]["type"] == "SOURCE_INCOMPLETE"


@pytest.mark.parametrize(
    "order",
    [
        (),
        DELIVERY[:-1],
        (*DELIVERY, DELIVERY[0]),
        (DELIVERY[0], *DELIVERY[:-1]),
        ("profit", *DELIVERY[1:]),
        (True, *DELIVERY[1:]),
    ],
)
def test_custom_order_rejects_missing_duplicate_unknown_or_non_string_objectives(order):
    with pytest.raises(ValidationError):
        ObjectiveDefinition(
            selection="custom",
            objective_order=order,
            max_weighted_tardiness=0,
            max_incremental_overtime_minutes=0,
        )


def test_custom_reorders_stability_ties_without_lowering_protected_priorities():
    order = (DELIVERY[0], DELIVERY[1], DELIVERY[3], DELIVERY[2], DELIVERY[4])
    definition = ObjectiveDefinition(selection="custom", objective_order=order)
    assert definition.order == order and definition.bounds == {}


def test_custom_makespan_first_keeps_both_explicit_bounds_and_negative_incremental_overtime():
    definition = ObjectiveDefinition(
        selection="custom",
        objective_order=("makespan", *DELIVERY[:-1]),
        max_weighted_tardiness=15,
        max_incremental_overtime_minutes=-20,
    )
    assert definition.bounds == {"weighted_tardiness": 15, "incremental_overtime_metric": -20}
    assert definition.order[0] == "makespan"


@pytest.mark.parametrize("value", [True, False, "5", 1.5, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["max_weighted_tardiness", "max_incremental_overtime_minutes"])
def test_bounds_require_exact_integer_minutes(field, value):
    with pytest.raises(ValidationError):
        ObjectiveDefinition(**{field: value})


def test_tardiness_cannot_be_negative_but_incremental_overtime_can():
    with pytest.raises(ValidationError):
        ObjectiveDefinition(max_weighted_tardiness=-1)
    definition = ObjectiveDefinition(max_incremental_overtime_minutes=-1)
    assert definition.bounds == {"incremental_overtime_metric": -1}


def test_large_integer_bounds_are_preserved_for_exact_solver_and_checker_comparison():
    boundary = 10**40
    definition = ObjectiveDefinition(
        max_weighted_tardiness=boundary, max_incremental_overtime_minutes=-boundary
    )
    assert definition.bounds == {
        "weighted_tardiness": boundary,
        "incremental_overtime_metric": -boundary,
    }
    assert ObjectiveDefinition.model_validate_json(definition.model_dump_json()) == definition


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("weights", {"weighted_tardiness": 10}),
        ("currency_rate", 1),
        ("overtime_cost_per_minute", 1),
        ("rates", {"worker": 2}),
        ("overtime_metric", "cost"),
        ("units", {"weighted_tardiness": "hours"}),
        ("confirmed", True),
        ("allow_overtime", True),
        ("disable_hard_deadline", True),
    ],
)
def test_definitions_cannot_introduce_costs_weights_permissions_or_new_units(field, value):
    with pytest.raises(ValidationError):
        ObjectiveDefinition(**{field: value})


def test_hash_and_derived_version_bind_exact_definition_sources_and_configuration(snapshot):
    effective = EffectiveObjective(**objective_data(snapshot))
    effective.validate_for(snapshot)
    data = effective.model_dump(mode="json", exclude={"content_hash"})
    expected = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert effective.content_hash == expected
    assert effective.objective_version == "objective:" + expected
    assert effective.objective_version != "delivery-v1"
    assert effective.order == DELIVERY and effective.bounds == {}
    reread = EffectiveObjective.model_validate_json(effective.model_dump_json())
    assert reread == effective and reread.content_hash == expected


def test_unknown_bounds_stay_null_and_copied_convenience_mapping_does_not_mutate_contract(snapshot):
    effective = EffectiveObjective(
        **objective_data(snapshot, definition={"max_incremental_overtime_minutes": 0})
    )
    serialized = effective.model_dump(mode="json")
    assert serialized["definition"]["max_weighted_tardiness"] is None
    assert serialized["definition"]["max_incremental_overtime_minutes"] == 0
    digest = effective.content_hash
    bounds = effective.bounds
    bounds["weighted_tardiness"] = 900
    assert effective.bounds == {"incremental_overtime_metric": 0}
    assert effective.content_hash == digest


@pytest.mark.parametrize(
    "change", ["definition", "source_version", "confirmer", "time", "coordination"]
)
def test_tampering_hash_bound_content_is_rejected(snapshot, change):
    data = EffectiveObjective(**objective_data(snapshot)).model_dump(mode="json")
    if change == "definition":
        data["definition"]["max_weighted_tardiness"] = 1
    elif change == "source_version":
        data["sources"][0]["version"] = 2
    elif change == "confirmer":
        data["sources"][0]["confirmed_by"] = "another-planner"
    elif change == "time":
        data["sources"][0]["confirmed_at"] = "2026-09-17T00:01:00Z"
    else:
        data["coordination_id"] = "coordination-2"
    with pytest.raises(ValidationError) as caught:
        EffectiveObjective.model_validate(data)
    assert caught.value.errors()[0]["type"] == "HASH_MISMATCH"


def test_validate_for_rechecks_hash_even_for_unvalidated_model_copy(snapshot):
    original = EffectiveObjective(**objective_data(snapshot))
    forged = original.model_copy(
        update={"definition": ObjectiveDefinition(max_weighted_tardiness=9)}
    )
    with pytest.raises(ValidationError) as caught:
        forged.validate_for(snapshot)
    assert caught.value.errors()[0]["type"] == "HASH_MISMATCH"


def test_nested_models_and_global_units_are_immutable(snapshot):
    effective = EffectiveObjective(**objective_data(snapshot))
    with pytest.raises(ValidationError, match="frozen"):
        effective.policy_version = "changed"
    with pytest.raises(ValidationError, match="frozen"):
        effective.definition.max_weighted_tardiness = 3
    with pytest.raises(ValidationError, match="frozen"):
        effective.sources[0].version = 2
    with pytest.raises(TypeError):
        METRIC_UNITS["weighted_tardiness"] = "money"


@pytest.mark.parametrize("field", ["factory_id", "profile_version", "policy_version"])
def test_validate_for_rejects_foreign_or_changed_configuration(snapshot, field):
    data = objective_data(snapshot, **{field: "another-version-or-factory"})
    if field == "factory_id":
        data["sources"][0]["scope_id"] = data[field]
    with pytest.raises(ValueError) as caught:
        EffectiveObjective(**data).validate_for(snapshot)
    assert caught.value.type == (
        "INVALID_REFERENCE" if field == "factory_id" else "VERSION_MISMATCH"
    )


def test_definition_and_confirmation_changes_require_new_version_without_mutating_snapshot(
    snapshot,
):
    before = snapshot.model_dump_json()
    original = EffectiveObjective(**objective_data(snapshot))
    newer = EffectiveObjective(
        **objective_data(
            snapshot,
            definition={"selection": "overtime_first", "max_weighted_tardiness": 0},
            sources=[source_data(snapshot, version=2)],
        )
    )
    assert newer.objective_version != original.objective_version
    newer.validate_for(snapshot)
    original.validate_for(snapshot)
    assert snapshot.model_dump_json() == before


def test_process_source_uses_explicit_product_and_route_pair(snapshot):
    product = snapshot.profile.products[0]
    process = source_data(
        snapshot,
        scope_type="PROCESS",
        scope_id="assembly-default",
        product_id=product.product_id,
        route_version=product.route_version,
    )
    effective = EffectiveObjective(**objective_data(snapshot, sources=[process]))
    effective.validate_for(snapshot)
    for key in ("product_id", "route_version"):
        invalid = {**process, key: "unknown"}
        with pytest.raises(ValueError) as caught:
            EffectiveObjective(**objective_data(snapshot, sources=[invalid])).validate_for(snapshot)
        assert caught.value.type == "INVALID_REFERENCE"


@pytest.mark.parametrize(
    "updates",
    [
        {"scope_type": "PROCESS"},
        {"scope_type": "PROCESS", "product_id": "product"},
        {"scope_type": "PROCESS", "route_version": "route"},
        {"product_id": "product", "route_version": "route"},
        {"scope_type": "CASE", "product_id": "product", "route_version": "route"},
        {"version": True},
        {"version": 0},
        {"confirmed_at": "2026-09-17T00:00:00"},
        {"confirmed_at": 123456},
        {"confirmed_by": ""},
        {"clock": "simulation"},
        {"confirmed": True},
    ],
)
def test_invalid_scope_confirmation_or_version_is_rejected(snapshot, updates):
    with pytest.raises(ValidationError):
        ObjectiveSource(**source_data(snapshot, **updates))


def test_confirmation_uses_real_clock_and_normalizes_equivalent_timezones(snapshot):
    first = EffectiveObjective(**objective_data(snapshot))
    second = EffectiveObjective(
        **objective_data(
            snapshot, sources=[source_data(snapshot, confirmed_at="2026-09-17T00:00:00Z")]
        )
    )
    assert first.content_hash == second.content_hash
    assert first.sources[0].confirmed_at > snapshot.snapshot_clock
    first.validate_for(snapshot)


@pytest.mark.parametrize("variant", ["none", "same_id", "same_scope", "foreign_factory"])
def test_missing_ambiguous_or_foreign_provenance_is_rejected(snapshot, variant):
    one = source_data(snapshot)
    two = source_data(snapshot, preference_id="preference-2", scope_type="CASE", scope_id="case-1")
    if variant == "none":
        sources = []
    elif variant == "same_id":
        two["preference_id"] = one["preference_id"]
        sources = [one, two]
    elif variant == "same_scope":
        sources = [one, {**one, "preference_id": "preference-2", "version": 2}]
    else:
        sources = [{**one, "scope_id": "foreign-factory"}]
    with pytest.raises(ValidationError):
        EffectiveObjective(**objective_data(snapshot, sources=sources))


def test_two_confirmed_cases_can_share_one_definition_without_fabricated_coordination(snapshot):
    sources = [
        source_data(
            snapshot,
            preference_id=f"preference-{index}",
            scope_type="CASE",
            scope_id=f"case-{index}",
        )
        for index in (1, 2)
    ]
    effective = EffectiveObjective(**objective_data(snapshot, sources=sources))
    effective.validate_for(snapshot)
    assert len(effective.sources) == 2 and effective.coordination_id is None


def test_original_v1_snapshot_and_candidate_hashes_and_delivery_version_are_unchanged(snapshot):
    snapshot_bytes = (ROOT / "data/development/skf-small.json").read_bytes()
    candidate_bytes = (ROOT / "tests/fixtures/contracts/p1-candidate.json").read_bytes()
    original_snapshot, original_candidate = json.loads(snapshot_bytes), json.loads(candidate_bytes)
    candidate = Candidate.model_validate_json(candidate_bytes)
    EffectiveObjective(**objective_data(snapshot)).validate_for(snapshot)
    assert snapshot.content_hash == original_snapshot["content_hash"]
    assert candidate.content_hash == original_candidate["content_hash"]
    assert candidate.binding.objective_version == "delivery-v1"
    assert (
        Snapshot.model_validate_json(snapshot.model_dump_json()).content_hash
        == snapshot.content_hash
    )
    assert (
        Candidate.model_validate_json(candidate.model_dump_json()).content_hash
        == candidate.content_hash
    )
    assert (ROOT / "data/development/skf-small.json").read_bytes() == snapshot_bytes
    assert (ROOT / "tests/fixtures/contracts/p1-candidate.json").read_bytes() == candidate_bytes


def test_version_is_derived_not_accepted_as_a_model_claim(snapshot):
    data = deepcopy(objective_data(snapshot))
    data["objective_version"] = "delivery-v1"
    with pytest.raises(ValidationError) as caught:
        EffectiveObjective(**data)
    assert caught.value.errors()[0]["type"] == "extra_forbidden"


def test_legacy_submission_keeps_original_nullable_fields_and_payload_hash(snapshot):
    candidate = Candidate.model_validate_json(
        (ROOT / "tests/fixtures/contracts/p1-candidate.json").read_bytes()
    )
    original = {
        "operation_id": "legacy-publication",
        "factory_id": snapshot.factory_id,
        "run_id": snapshot.run_id,
        "expected_source_revision": snapshot.source.source_revision,
        "expected_snapshot_hash": snapshot.content_hash,
        "expected_active_plan_version": None,
        "candidate": candidate.model_dump(mode="json"),
        "approvals": [],
    }
    submission = PlanSubmission.model_validate(original)
    assert submission.model_dump(mode="json") == original
    assert canonical_hash(submission) == canonical_hash(original)
    assert (
        PlanSubmission.model_validate({**original, "objective": None}).model_dump(mode="json")
        == original
    )


def test_return_to_defaults_gets_new_epoch_version_without_reviving_legacy_approval(snapshot):
    before = EffectiveObjective(**objective_data(snapshot, resolution_version=1))
    after = EffectiveObjective(**objective_data(snapshot, sources=[], resolution_version=2))
    next_default = EffectiveObjective(**objective_data(snapshot, sources=[], resolution_version=3))
    after.validate_for(snapshot)
    next_default.validate_for(snapshot)
    assert after.order == before.order == DELIVERY
    assert after.bounds == {} and after.sources == ()
    assert (
        len(
            {
                before.objective_version,
                after.objective_version,
                next_default.objective_version,
                "delivery-v1",
            }
        )
        == 4
    )


@pytest.mark.parametrize("value", [-1, True, "2", 1.5])
def test_resolution_epoch_is_an_explicit_nonnegative_integer(snapshot, value):
    with pytest.raises(ValidationError):
        EffectiveObjective(**objective_data(snapshot, resolution_version=value))


@pytest.mark.parametrize(
    "definition",
    [
        {"max_weighted_tardiness": 0},
        {"max_incremental_overtime_minutes": 0},
        {"selection": "overtime_first", "max_weighted_tardiness": 0},
        {"selection": "custom", "objective_order": DELIVERY},
    ],
)
def test_empty_sources_cannot_authorize_custom_order_or_new_bounds(snapshot, definition):
    with pytest.raises(ValidationError) as caught:
        EffectiveObjective(
            **objective_data(snapshot, sources=[], resolution_version=1, definition=definition)
        )
    assert caught.value.errors()[0]["type"] == "CONFIRMATION_REQUIRED"
