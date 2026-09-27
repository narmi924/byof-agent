"""Pure onboarding checks distinguish supported drafts from real activation authority."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.domain.models import ConnectorCapabilities, Snapshot, canonical_hash
from packages.domain.objectives import ObjectiveDefinition
from packages.domain.onboarding import (
    INFORMATION_FIELDS,
    OnboardingDraft,
    OnboardingValidation,
    PolicyBundle,
    validate_onboarding,
)
from packages.domain.skf import load_skf_snapshot
from tests.test_connector_mapping import mapping_for


def supported_capabilities():
    return {
        "read_snapshot": True,
        "read_changes": True,
        "query_detail": True,
        "accept_plan": True,
        "query_action": True,
        "idempotency": True,
        "conditional_acceptance": True,
        "snapshot_consistency": "ATOMIC_SNAPSHOT",
    }


def draft_data(snapshot):
    mapping = mapping_for(snapshot).model_dump(mode="json")
    mapping["endpoint_bindings"].extend(
        [
            {"operation": "query_detail", "endpoint_id": "details"},
            {"operation": "accept_plan", "endpoint_id": "plans"},
            {"operation": "query_action", "endpoint_id": "actions"},
        ]
    )
    return {
        "factory_id": snapshot.factory_id,
        "profile": snapshot.profile.model_dump(mode="json"),
        "mapping": mapping,
        "policy": {
            "bundle_id": "management-policy",
            "factory_id": snapshot.factory_id,
            "version": 1,
            "profile_version": snapshot.profile.version,
            "planning_policy_version": snapshot.profile.policy.policy_version,
            "default_objective": {"selection": "delivery_first"},
            "allowed_scenarios": ["regular", "overtime"],
            "information_owners": [
                {"field": "repair_eta", "role": "maintainer"},
                {"field": "remaining_minutes", "role": "team_lead"},
                {"field": "remaining_setup_minutes", "role": "team_lead"},
                {"field": "receipt_eta", "role": "warehouse"},
                {"field": "comment", "role": "planner"},
            ],
            "information_deadline_minutes": 30,
            "notification_channel": "workbench",
        },
        "evidence": {
            "bundle_id": "evidence",
            "factory_id": snapshot.factory_id,
            "version": 1,
            "records": [
                {
                    "evidence_id": "declared-input-source",
                    "source_id": snapshot.source.source_system,
                    "document_version": snapshot.profile.version,
                    "content_digest": snapshot.profile.source_digest,
                    "classification": "SYNTHETIC",
                    "supports_fields": ["profile", "mapping", "policy"],
                    "limitations": "The operating parameters of this example are declared synthetic input; classification and summaries are not manual confirmation.",
                }
            ],
            "unresolved_fields": [],
        },
    }


@pytest.fixture
def skf():
    return load_skf_snapshot(development=True)


def codes(result):
    return {item.code for item in result.findings}


def check(raw, snapshot, capabilities=None):
    return validate_onboarding(
        raw,
        sample_snapshot=snapshot,
        capabilities=supported_capabilities() if capabilities is None else capabilities,
    )


@pytest.mark.parametrize("kind", ["skf", "branch"])
def test_supported_four_bundle_draft_is_serializable_but_never_confirms_or_activates(kind):
    snapshot = (
        load_skf_snapshot(development=True)
        if kind == "skf"
        else Snapshot.model_validate_json(
            (
                Path(__file__).resolve().parents[1] / "data/development/assembly-branch.json"
            ).read_bytes()
        )
    )
    raw = draft_data(snapshot)
    retained = deepcopy(raw)
    draft = OnboardingDraft.model_validate(raw)
    result = check(draft, snapshot)
    assert result.state == "NEEDS_CONFIRMATION" and result.activation_allowed is False
    assert result.execution_mode == "CONDITIONAL"
    assert result.draft_hash == canonical_hash(draft)
    assert result.snapshot_hash == snapshot.content_hash
    assert not any(item.severity == "ERROR" for item in result.findings)
    assert result.pending_checks == (
        "registered_connection",
        "runtime_sample",
        "administrator_confirmation",
        "contact_authority",
    )
    assert "ADMINISTRATOR_CONFIRMATION_REQUIRED" in codes(result)
    assert OnboardingValidation.model_validate_json(result.model_dump_json()) == result
    assert raw == retained and draft.state == draft.profile.activation_state == "DRAFT"


def test_policy_consumable_values_match_current_objectives_roles_clocks_and_limits(skf):
    policy = PolicyBundle.model_validate(draft_data(skf)["policy"])
    assert policy.default_objective == ObjectiveDefinition()
    assert policy.allowed_scenarios == ("regular", "overtime")
    assert (policy.publish_role, policy.overtime_role, policy.configuration_role) == (
        "planner",
        "manager",
        "admin",
    )
    assert {item.field for item in policy.information_owners} == INFORMATION_FIELDS
    assert policy.information_deadline_minutes == 30
    assert (
        policy.approval_ttl_minutes,
        policy.reminder_interval_minutes,
        policy.reminder_limit,
        policy.clock,
    ) == (60, 15, 2, "real")


def test_bounded_stability_objective_is_only_a_draft_preference(skf):
    raw = draft_data(skf)
    raw["policy"]["default_objective"] = {
        "selection": "stability_first",
        "max_weighted_tardiness": 20,
        "max_incremental_overtime_minutes": 0,
    }
    result = check(raw, skf)
    assert result.state == "NEEDS_CONFIRMATION" and result.activation_allowed is False
    assert (
        OnboardingDraft.model_validate(raw).policy.default_objective.order[0]
        == "changed_operations"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("publish_role", "manager"),
        ("overtime_role", "planner"),
        ("configuration_role", "planner"),
        ("allowed_scenarios", ["ignore_quality"]),
        ("allowed_scenarios", ["overtime"]),
        ("allowed_scenarios", ["regular", "regular"]),
        ("information_deadline_minutes", 0),
        ("information_deadline_minutes", 1441),
        ("information_deadline_minutes", True),
        ("information_deadline_minutes", 30.0),
        ("approval_ttl_minutes", 120),
        ("approval_ttl_minutes", 60.0),
        ("reminder_interval_minutes", 0),
        ("reminder_limit", 100),
        ("clock", "business"),
        ("notification_channel", "webhook"),
        ("default_objective", {"selection": "stability_first"}),
        ("default_objective", {"weights": {"profit": 5}}),
    ],
)
def test_policy_cannot_widen_roles_deadlines_scenarios_or_unimplemented_objectives(
    skf, field, value
):
    raw = draft_data(skf)
    raw["policy"][field] = value
    result = check(raw, skf)
    assert result.state == "BLOCKED" and result.activation_allowed is False
    assert any(item.severity == "ERROR" and item.path[0] == "policy" for item in result.findings)


@pytest.mark.parametrize(
    "kind", ["admin_role", "simulator_role", "duplicate_field", "unsupported_field"]
)
def test_information_assignment_is_explicit_and_never_grants_source_write_authority(skf, kind):
    raw = draft_data(skf)
    owner = raw["policy"]["information_owners"][0]
    if kind in {"admin_role", "simulator_role"}:
        owner["role"] = "admin" if kind == "admin_role" else "sim_admin"
    elif kind == "duplicate_field":
        owner["field"] = "comment"
    else:
        owner["field"] = "post_inventory"
    assert check(raw, skf).state == "BLOCKED"


@pytest.mark.parametrize("target", ["profile", "mapping", "policy", "evidence"])
def test_four_bundles_cannot_mix_factory_scopes(skf, target):
    raw = draft_data(skf)
    raw[target]["factory_id"] = "another-factory"
    result = check(raw, skf)
    assert result.state == "BLOCKED" and "INVALID_REFERENCE" in codes(result)


@pytest.mark.parametrize("field", ["profile_version", "planning_policy_version"])
def test_policy_is_bound_to_exact_profile_and_physical_policy_version(skf, field):
    raw = draft_data(skf)
    raw["policy"][field] = "other-version"
    result = check(raw, skf)
    assert result.state == "BLOCKED" and "VERSION_MISMATCH" in codes(result)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("unit", "UNKNOWN_UNIT"),
        ("target_unit", "UNKNOWN_UNIT"),
        ("missing_duration", "MISSING_DURATION"),
        ("cycle", "CYCLIC_ROUTE"),
        ("unimplemented", "UNSUPPORTED_CAPABILITY"),
        ("two_workers", "UNSUPPORTED_CAPABILITY"),
        ("inventory_semantics", "UNSUPPORTED_INVENTORY_SEMANTICS"),
        ("unmapped_fields", "INCOMPLETE_FIELD_MAPPING"),
        ("unmapped_status", "MISSING_STATUS_MAPPING"),
        ("unmapped_unit", "MISSING_UNIT_MAPPING"),
        ("path_overlap", "AMBIGUOUS_MAPPING_PATH"),
        ("monetary", "MONETARY_OBJECTIVE_UNSUPPORTED"),
    ],
)
def test_unsupported_factory_and_mapping_semantics_are_explicit_blockers(skf, kind, expected):
    raw = draft_data(skf)
    if kind == "unit":
        raw["profile"]["materials"][0]["unit"] = "kg"
    elif kind == "target_unit":
        raw["mapping"]["units"][0]["target_unit"] = "kg"
    elif kind == "missing_duration":
        del raw["profile"]["routes"][0]["cycle_sec_per_unit"]
    elif kind == "cycle":
        row = raw["profile"]["routes"][0]
        row["predecessors"] = [row["step_id"]]
    elif kind == "unimplemented":
        raw["profile"]["required_capabilities"].append("continuous_process_reactor")
    elif kind == "two_workers":
        raw["profile"]["policy"]["workers_per_operation"] = 2
    elif kind == "inventory_semantics":
        raw["mapping"]["inventory_reserved_semantics"] = "unknown"
    elif kind == "unmapped_fields":
        raw["mapping"]["fields"].pop()
    elif kind == "unmapped_status":
        raw["mapping"]["statuses"] = []
    elif kind == "unmapped_unit":
        raw["mapping"]["units"] = [
            row for row in raw["mapping"]["units"] if row["target_unit"] != "EA"
        ]
    elif kind == "path_overlap":
        raw["mapping"]["fields"][0]["source_field"] = "identity"
        raw["mapping"]["fields"][1]["source_field"] = "identity.product"
    else:
        raw["profile"]["policy"]["overtime_cost_per_minute"] = 0
    result = check(raw, skf)
    assert result.state == "BLOCKED" and expected in codes(result)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("worker", "MISSING_RESOURCE_OR_SKILL"),
        ("resource", "MISSING_RESOURCE_OR_SKILL"),
        ("calendar", "INVALID_TIME"),
        ("stale", "SOURCE_INCOMPLETE"),
        ("incomplete", "SOURCE_INCOMPLETE"),
        ("source", "SAMPLE_SOURCE_MISMATCH"),
    ],
)
def test_actual_sample_must_supply_qualified_resources_skills_calendars_and_current_facts(
    skf, kind, expected
):
    sample = skf.model_dump(mode="json", exclude={"content_hash"})
    if kind == "worker":
        for row in sample["workers"]:
            row["skills"] = ["UNRELATED"]
    elif kind == "resource":
        for row in sample["resources"]:
            row["operation_codes"] = ["UNRELATED"]
    elif kind == "calendar":
        sample["workers"][0]["calendar"] = []
    elif kind == "stale":
        sample["source"]["freshness"] = "STALE"
    elif kind == "incomplete":
        sample["source"]["complete"] = False
    else:
        sample["source"]["source_system"] = "other-source"
    result = check(draft_data(skf), sample)
    assert result.state == "BLOCKED" and expected in codes(result)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("unresolved", "UNRESOLVED_EVIDENCE"),
        ("unknown", "UNKNOWN_EVIDENCE"),
        ("wrong_digest", "PROFILE_EVIDENCE_MISSING"),
        ("missing_scope", "EVIDENCE_SCOPE_REQUIRED"),
        ("bad_reference", "EVIDENCE_REFERENCE_INVALID"),
    ],
)
def test_missing_unknown_or_wrong_evidence_cannot_become_activation_proof(skf, kind, expected):
    raw = draft_data(skf)
    record = raw["evidence"]["records"][0]
    if kind == "unresolved":
        raw["evidence"]["unresolved_fields"] = ["profile.routes.0.cycle_sec_per_unit"]
    elif kind == "unknown":
        record["classification"] = "UNKNOWN"
    elif kind == "wrong_digest":
        record["content_digest"] = "0" * 64
    elif kind == "missing_scope":
        record["supports_fields"] = []
    else:
        record["supports_fields"] = ["profile.routes.999.cycle_sec_per_unit", "evidence.records.0"]
    result = check(raw, skf)
    assert result.state == "BLOCKED" and expected in codes(result)


def test_declared_unknown_quality_threshold_does_not_fabricate_a_number_or_block_supported_profile(
    skf,
):
    assert all(row.quality_threshold is None for row in skf.profile.routes)
    raw = draft_data(skf)
    raw["evidence"]["records"][0]["supports_fields"].append("profile.routes.0.quality_threshold")
    assert check(raw, skf).state == "NEEDS_CONFIRMATION"
    assert all(row["quality_threshold"] is None for row in raw["profile"]["routes"])


def test_read_only_connection_explicitly_degrades_without_blocking_a_valid_draft(skf):
    declared = supported_capabilities()
    declared.update(
        accept_plan=False, conditional_acceptance=False, query_action=False, idempotency=False
    )
    result = check(draft_data(skf), skf, declared)
    assert result.state == "NEEDS_CONFIRMATION" and result.execution_mode == "EXPORT_ONLY"
    assert "EXECUTION_EXPORT_ONLY" in codes(result)
    assert result.activation_allowed is False


def test_mapping_cannot_infer_write_support_from_unbound_source_capabilities(skf):
    raw = draft_data(skf)
    raw["mapping"]["endpoint_bindings"] = [
        row for row in raw["mapping"]["endpoint_bindings"] if row["operation"] == "read_snapshot"
    ]
    result = check(raw, skf)
    assert result.state == "NEEDS_CONFIRMATION" and result.execution_mode == "EXPORT_ONLY"
    assert "EXECUTION_EXPORT_ONLY" in codes(result)


def test_unverified_consistency_and_missing_snapshot_binding_block_activation(skf):
    declared = supported_capabilities()
    declared["snapshot_consistency"] = "UNVERIFIED"
    raw = draft_data(skf)
    raw["mapping"]["endpoint_bindings"] = [
        row for row in raw["mapping"]["endpoint_bindings"] if row["operation"] != "read_snapshot"
    ]
    result = check(raw, skf, declared)
    assert result.state == "BLOCKED"
    assert {"SOURCE_CONSISTENCY_REQUIRED", "MISSING_SNAPSHOT_ENDPOINT"} <= codes(result)


def test_progress_revalidation_requires_incremental_evidence_capability_and_binding(skf):
    sample = skf.model_dump(mode="json", exclude={"content_hash"})
    sample["profile"]["policy"]["progress_revalidation_enabled"] = True
    snapshot = Snapshot.model_validate(sample)
    raw = draft_data(snapshot)
    declared = supported_capabilities()
    declared["read_changes"] = False
    result = check(raw, snapshot, declared)
    assert result.state == "BLOCKED" and "PROGRESS_EVIDENCE_UNAVAILABLE" in codes(result)
    raw["mapping"]["endpoint_bindings"] = [
        row for row in raw["mapping"]["endpoint_bindings"] if row["operation"] != "read_changes"
    ]
    assert "PROGRESS_EVIDENCE_UNAVAILABLE" in codes(check(raw, snapshot))


def test_sample_consistency_cannot_be_upgraded_by_a_stronger_capability_claim(skf):
    sample = skf.model_dump(mode="json", exclude={"content_hash"})
    sample["source"]["consistency"] = "VERIFIED_WATERMARK"
    result = check(draft_data(skf), sample)
    assert result.state == "BLOCKED"
    assert "CONSISTENCY_DECLARATION_MISMATCH" in codes(result)
    declared = supported_capabilities()
    declared["snapshot_consistency"] = "VERIFIED_WATERMARK"
    assert check(draft_data(skf), sample, declared).state == "NEEDS_CONFIRMATION"


@pytest.mark.parametrize("minutes", [1, 1440])
def test_existing_real_clock_information_deadline_limits_are_accepted(skf, minutes):
    raw = draft_data(skf)
    raw["policy"]["information_deadline_minutes"] = minutes
    result = check(raw, skf)
    assert result.state == "NEEDS_CONFIRMATION" and result.activation_allowed is False


def test_sample_must_match_the_exact_proposed_profile_not_just_its_version_label(skf):
    raw = draft_data(skf)
    raw["profile"]["routes"][0]["cycle_sec_per_unit"] += 1
    result = check(raw, skf)
    assert result.state == "BLOCKED" and "SAMPLE_PROFILE_MISMATCH" in codes(result)


@pytest.mark.parametrize("target", ["origin", "endpoint"])
def test_configuration_only_references_registered_names_not_network_addresses(skf, target):
    raw = draft_data(skf)
    if target == "origin":
        raw["mapping"]["origin_id"] = "http://169.254.169.254/metadata"
    else:
        raw["mapping"]["endpoint_bindings"][0]["endpoint_id"] = "https://example.invalid/private"
    result = check(raw, skf)
    assert result.state == "BLOCKED" and result.activation_allowed is False


def test_email_selection_neither_sends_nor_certifies_contact_identity(skf):
    raw = draft_data(skf)
    raw["policy"]["notification_channel"] = "workbench_and_email"
    result = check(raw, skf)
    assert result.state == "NEEDS_CONFIRMATION" and result.activation_allowed is False
    assert "EMAIL_CHANNEL_CHECK_REQUIRED" in codes(result)
    assert "contact_authority" in result.pending_checks


@pytest.mark.parametrize(
    "kind",
    ["confirmed", "confirmed_by", "activation_allowed", "active", "validated", "script", "secret"],
)
def test_ai_cannot_import_confirmations_or_executable_configuration_and_errors_hide_values(
    skf, kind
):
    raw = draft_data(skf)
    sentinel = "private-value-must-not-appear"
    if kind in {"confirmed", "activation_allowed"}:
        raw[kind] = True
    elif kind == "confirmed_by":
        raw[kind] = sentinel
    elif kind == "active":
        raw["state"] = "ACTIVE"
    elif kind == "validated":
        raw["profile"]["activation_state"] = "VALIDATED"
    elif kind == "script":
        raw["policy"]["python"] = sentinel
    else:
        raw["mapping"]["api_key"] = sentinel
    result = check(raw, skf)
    assert result.state == "BLOCKED" and result.activation_allowed is False
    assert sentinel not in result.model_dump_json()
    assert result.draft_hash is None


def test_missing_sample_and_server_capabilities_remain_explicit_blockers(skf):
    result = validate_onboarding(draft_data(skf))
    assert result.state == "BLOCKED"
    assert {"SAMPLE_SNAPSHOT_REQUIRED", "CAPABILITY_CHECK_REQUIRED"} <= codes(result)
    assert result.execution_mode == "UNVERIFIED" and result.snapshot_hash is None


def test_false_capability_input_cannot_coerce_strings_or_boolean_flags(skf):
    declared = supported_capabilities()
    declared["accept_plan"] = "true"
    assert check(draft_data(skf), skf, declared).state == "BLOCKED"
    with pytest.raises(ValidationError):
        ConnectorCapabilities.model_validate(declared)


def test_invalid_raw_bundle_is_a_structured_blocked_result_not_an_exception(skf):
    for raw in (None, [], "arbitrary code", {"profile": "untrusted"}):
        result = check(raw, skf)
        assert result.state == "BLOCKED" and result.draft_hash is None
        assert json.loads(result.model_dump_json())["activation_allowed"] is False
