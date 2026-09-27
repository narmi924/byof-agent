"""Four configuration documents can be checked without granting activation authority."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import Field, StrictInt, StrictStr, ValidationError, model_validator

from packages.domain.models import (
    ConnectorCapabilities,
    ConnectorMapping,
    Contract,
    Digest,
    ErrorCode,
    EvidenceBundle,
    FactoryProfile,
    Identifier,
    Inventory,
    Order,
    Positive,
    Receipt,
    Resource,
    Snapshot,
    Worker,
    canonical_hash,
    reject,
)
from packages.domain.objectives import ObjectiveDefinition

InformationField = Literal[
    "repair_eta", "remaining_minutes", "remaining_setup_minutes", "receipt_eta", "comment"
]
InformationRole = Literal["maintainer", "warehouse", "team_lead", "planner", "manager"]
INFORMATION_FIELDS = frozenset(
    {"repair_eta", "remaining_minutes", "remaining_setup_minutes", "receipt_eta", "comment"}
)
# Keep explicit: a newly named capability must not become supported merely by parsing it.
SUPPORTED_CAPABILITIES = frozenset(
    {
        "fixed_lot_exact_split",
        "acyclic_operation_precedence",
        "alternative_unary_resource",
        "one_qualified_worker_full_duration",
        "time_phased_supply",
        "full_kit_before_first_operation",
        "per_operation_consumption",
        "freeze_and_confirmed_wip",
        "independent_schedule_check",
        "versioned_human_approval",
    }
)
ENTITY_TYPES: dict[str, type[Contract]] = {
    "order": Order,
    "inventory": Inventory,
    "receipt": Receipt,
    "resource": Resource,
    "worker": Worker,
}


class InformationOwner(Contract):
    field: InformationField
    role: InformationRole


class PolicyBundle(Contract):
    schema_version: Literal["byof.policy-bundle/1"] = "byof.policy-bundle/1"
    bundle_id: Identifier
    factory_id: Identifier
    version: Positive
    profile_version: Identifier
    planning_policy_version: Identifier
    default_objective: ObjectiveDefinition
    allowed_scenarios: Annotated[
        tuple[Literal["regular", "overtime"], ...], Field(min_length=1, max_length=2)
    ]
    publish_role: Literal["planner"] = "planner"
    overtime_role: Literal["manager"] = "manager"
    configuration_role: Literal["admin"] = "admin"
    information_owners: Annotated[tuple[InformationOwner, ...], Field(min_length=5, max_length=5)]
    information_deadline_minutes: Annotated[StrictInt, Field(ge=1, le=1440)]
    notification_channel: Literal["workbench", "workbench_and_email"]
    approval_ttl_minutes: Annotated[StrictInt, Field(ge=60, le=60)] = 60
    reminder_interval_minutes: Annotated[StrictInt, Field(ge=15, le=15)] = 15
    reminder_limit: Annotated[StrictInt, Field(ge=2, le=2)] = 2
    clock: Literal["real"] = "real"

    @model_validator(mode="after")
    def supported_policy(self) -> Self:
        if len(set(self.allowed_scenarios)) != len(self.allowed_scenarios):
            reject(ErrorCode.DUPLICATE_ID, "Planning scenarios must be distinct")
        if "regular" not in self.allowed_scenarios:
            reject(ErrorCode.UNSUPPORTED_CAPABILITY, "The regular planning scenario is required")
        if {item.field for item in self.information_owners} != INFORMATION_FIELDS:
            reject(ErrorCode.SOURCE_INCOMPLETE, "Each supported information field needs one owner")
        return self


class OnboardingDraft(Contract):
    schema_version: Literal["byof.onboarding-draft/1"] = "byof.onboarding-draft/1"
    state: Literal["DRAFT"] = "DRAFT"
    factory_id: Identifier
    profile: FactoryProfile
    mapping: ConnectorMapping
    policy: PolicyBundle
    evidence: EvidenceBundle

    @model_validator(mode="after")
    def matching_documents(self) -> Self:
        if self.profile.activation_state != "DRAFT":
            reject(ErrorCode.CONFIRMATION_REQUIRED, "Imported profiles are unconfirmed drafts")
        if any(
            value.factory_id != self.factory_id
            for value in (self.profile, self.mapping, self.policy, self.evidence)
        ):
            reject(ErrorCode.INVALID_REFERENCE, "All configuration documents must share a factory")
        if (
            self.policy.profile_version != self.profile.version
            or self.policy.planning_policy_version != self.profile.policy.policy_version
        ):
            reject(ErrorCode.VERSION_MISMATCH, "Management policy must bind these profile versions")
        return self


class ValidationFinding(Contract):
    severity: Literal["ERROR", "WARNING", "INFO"]
    code: Identifier
    path: tuple[StrictStr | StrictInt, ...]
    message: Annotated[StrictStr, Field(min_length=1, max_length=300)]


class OnboardingValidation(Contract):
    state: Literal["BLOCKED", "NEEDS_CONFIRMATION"]
    draft_hash: Digest | None
    snapshot_hash: Digest | None
    execution_mode: Literal["UNVERIFIED", "EXPORT_ONLY", "CONDITIONAL"]
    findings: tuple[ValidationFinding, ...]
    activation_allowed: Literal[False] = False
    pending_checks: tuple[
        Literal[
            "registered_connection",
            "runtime_sample",
            "administrator_confirmation",
            "contact_authority",
        ],
        ...,
    ] = (
        "registered_connection",
        "runtime_sample",
        "administrator_confirmation",
        "contact_authority",
    )


def _errors(exc: ValidationError, prefix: tuple[str | int, ...] = ()) -> list[ValidationFinding]:
    findings = []
    for error in exc.errors(include_input=False, include_url=False):
        path = (*prefix, *error["loc"])
        code = error["type"]
        message = "A configuration field or reference does not meet the supported contract; check the location shown."
        if path and path[-1] in {"confirmed", "confirmed_by", "confirmed_at", "activation_allowed"}:
            code, message = (
                "AUTHORITY_NOT_IMPORTABLE",
                "A configuration proposal cannot provide a confirming identity or activation authority.",
            )
        elif path and path[-1] in {"state", "activation_state"}:
            code, message = (
                "DRAFT_REQUIRED",
                "Imported content can only be saved as an unconfirmed draft.",
            )
        elif path and path[-1] == "inventory_reserved_semantics":
            code, message = (
                "UNSUPPORTED_INVENTORY_SEMANTICS",
                "It must be declared that on-hand balances already include reserved quantities.",
            )
        elif path and path[-1] in {"unit", "target_unit"}:
            code, message = (
                "UNKNOWN_UNIT",
                "The unit has no explicit mapping to a supported unit of measure.",
            )
        elif code == "missing" and path and path[-1] in {"setup_min", "cycle_sec_per_unit"}:
            code, message = (
                "MISSING_DURATION",
                "The operation has no explicit fixed or per-unit time.",
            )
        elif code == "CYCLIC_ROUTE":
            message = "The route dependencies contain a cycle or a missing predecessor."
        elif code == "INVALID_REFERENCE" and "qualified resource or worker" in error["msg"]:
            code, message = (
                "MISSING_RESOURCE_OR_SKILL",
                "The route lacks a qualified machine or staff with the required skill.",
            )
        elif code == "UNSUPPORTED_CAPABILITY" or (
            path and path[-1] in {"capacity", "workers_per_operation"}
        ):
            code, message = (
                "UNSUPPORTED_CAPABILITY",
                "The configuration requires a production or management capability that is not supported yet.",
            )
        elif code == "extra_forbidden":
            code, message = (
                "UNSUPPORTED_FIELD",
                "This field has no supported configuration use and cannot be applied or silently ignored.",
            )
        findings.append(ValidationFinding(severity="ERROR", code=code, path=path, message=message))
    return findings


def _reference(document: dict[str, Any], path: str) -> bool:
    if not path or len(path.split(".")) > 16:
        return False
    current: object = document
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif (
            isinstance(current, list)
            and part.isascii()
            and part.isdecimal()
            and int(part) < len(current)
        ):
            current = current[int(part)]
        else:
            return False
    return True


def validate_onboarding(
    raw: object,
    *,
    sample_snapshot: object | None = None,
    capabilities: object | None = None,
) -> OnboardingValidation:
    """Check supplied evidence; persistent services must still verify identity, HTTP and activation."""
    findings: list[ValidationFinding] = []
    draft: OnboardingDraft | None = None
    snapshot: Snapshot | None = None
    declared: ConnectorCapabilities | None = None
    execution_mode: Literal["UNVERIFIED", "EXPORT_ONLY", "CONDITIONAL"] = "UNVERIFIED"

    def finding(code: str, path: tuple[str | int, ...], message: str, severity="ERROR") -> None:
        findings.append(ValidationFinding(severity=severity, code=code, path=path, message=message))

    try:
        draft = OnboardingDraft.model_validate(raw)
    except ValidationError as exc:
        findings.extend(_errors(exc))
    if sample_snapshot is None:
        finding(
            "SAMPLE_SNAPSHOT_REQUIRED",
            ("sample_snapshot",),
            "Source samples are needed to check actual resources, staff and calendars.",
        )
    else:
        try:
            snapshot = Snapshot.model_validate(sample_snapshot)
        except ValidationError as exc:
            findings.extend(_errors(exc, ("sample_snapshot",)))
    if capabilities is None:
        finding(
            "CAPABILITY_CHECK_REQUIRED",
            ("capabilities",),
            "The service must check the actual capability declaration of the connector.",
        )
    else:
        try:
            declared = ConnectorCapabilities.model_validate(capabilities)
        except ValidationError as exc:
            findings.extend(_errors(exc, ("capabilities",)))

    if draft is not None:
        profile, mapping = draft.profile, draft.mapping
        document = draft.model_dump(mode="json")
        for capability in sorted(set(profile.required_capabilities) - SUPPORTED_CAPABILITIES):
            finding(
                "UNSUPPORTED_CAPABILITY",
                ("profile", "required_capabilities"),
                "A required capability has no runtime support yet.",
            )
        if profile.policy.overtime_cost_per_minute is not None:
            finding(
                "MONETARY_OBJECTIVE_UNSUPPORTED",
                ("profile", "policy", "overtime_cost_per_minute"),
                "The current objective only supports overtime minutes; a money basis cannot be activated.",
            )
        for entity, model in ENTITY_TYPES.items():
            fields = [item for item in mapping.fields if item.entity == entity]
            # Match the connector's narrow legacy exception; other defaults stay explicit.
            targets = {item.target_field for item in fields}
            expected = set(model.model_fields)
            legacy_optional = {"requested_due_at"} if entity == "order" else set()
            if targets - expected or (expected - targets) - legacy_optional:
                finding(
                    "INCOMPLETE_FIELD_MAPPING",
                    ("mapping", "fields", entity),
                    "The standard field mapping of this kind of source record is incomplete.",
                )
            paths: list[tuple[str, ...]] = []
            for item in fields:
                path = tuple(item.source_field.split("."))
                if (
                    len(path) > 16
                    or any(not part for part in path)
                    or any(
                        path[: len(previous)] == previous or previous[: len(path)] == path
                        for previous in paths
                    )
                ):
                    finding(
                        "AMBIGUOUS_MAPPING_PATH",
                        ("mapping", "fields", entity, item.target_field),
                        "The source field path is unclear or overlaps another mapping.",
                    )
                paths.append(path)
            if "status" in model.model_fields and not any(
                item.entity == entity for item in mapping.statuses
            ):
                finding(
                    "MISSING_STATUS_MAPPING",
                    ("mapping", "statuses", entity),
                    "The mapping of source states must be declared explicitly.",
                )
        target_units = {item.target_unit for item in mapping.units}
        if any(material.unit not in target_units for material in profile.materials):
            finding(
                "MISSING_UNIT_MAPPING",
                ("mapping", "units"),
                "The material unit lacks an explicit source unit mapping.",
            )
        endpoints = {item.operation for item in mapping.endpoint_bindings}
        if "read_snapshot" not in endpoints:
            finding(
                "MISSING_SNAPSHOT_ENDPOINT",
                ("mapping", "endpoint_bindings"),
                "The current connection needs a registered full snapshot read endpoint.",
            )
        if draft.evidence.unresolved_fields:
            for unresolved in draft.evidence.unresolved_fields:
                finding(
                    "UNRESOLVED_EVIDENCE",
                    ("evidence", "unresolved_fields"),
                    "The material still has unconfirmed fields; complete or explicitly reject them before activation.",
                )
        if not any(
            item.content_digest == profile.source_digest and item.classification != "UNKNOWN"
            for item in draft.evidence.records
        ):
            finding(
                "PROFILE_EVIDENCE_MISSING",
                ("evidence", "records"),
                "Classified material consistent with the source summary of the factory configuration is missing.",
            )
        for index, record in enumerate(draft.evidence.records):
            if not record.supports_fields:
                finding(
                    "EVIDENCE_SCOPE_REQUIRED",
                    ("evidence", "records", index, "supports_fields"),
                    "The material must state the configuration fields it supports.",
                )
            if record.classification == "UNKNOWN":
                finding(
                    "UNKNOWN_EVIDENCE",
                    ("evidence", "records", index, "classification"),
                    "The source evidence is not classified or confirmed yet and cannot justify activation.",
                )
            for support_path in record.supports_fields:
                if not _reference(document, support_path) or support_path.split(".", 1)[0] not in {
                    "profile",
                    "mapping",
                    "policy",
                }:
                    finding(
                        "EVIDENCE_REFERENCE_INVALID",
                        ("evidence", "records", index, "supports_fields"),
                        "The configuration field referenced by the material does not exist or is not part of the onboarding configuration.",
                    )
        if snapshot is not None:
            if snapshot.factory_id != draft.factory_id or snapshot.profile != profile:
                finding(
                    "SAMPLE_PROFILE_MISMATCH",
                    ("sample_snapshot", "profile"),
                    "The source sample does not belong to the factory or configuration version being checked.",
                )
            if snapshot.source.source_system != mapping.source_id:
                finding(
                    "SAMPLE_SOURCE_MISMATCH",
                    ("sample_snapshot", "source"),
                    "The source sample does not match the source identity registered by the connector.",
                )
            if (
                not snapshot.source.complete
                or snapshot.source.consistency == "UNVERIFIED"
                or snapshot.source.freshness != "CURRENT"
            ):
                finding(
                    "SOURCE_INCOMPLETE",
                    ("sample_snapshot", "source"),
                    "The source sample lacks a complete, current and consistent fact guarantee.",
                )
        if declared is not None:
            if not declared.read_snapshot or declared.snapshot_consistency == "UNVERIFIED":
                finding(
                    "SOURCE_CONSISTENCY_REQUIRED",
                    ("capabilities",),
                    "The current connection cannot provide a checkable consistent snapshot.",
                )
            if (
                snapshot is not None
                and snapshot.source.consistency != declared.snapshot_consistency
            ):
                finding(
                    "CONSISTENCY_DECLARATION_MISMATCH",
                    ("capabilities", "snapshot_consistency"),
                    "The interface capability declaration does not match the consistency mode of the source sample.",
                )
            if profile.policy.progress_revalidation_enabled and (
                not declared.read_changes or "read_changes" not in endpoints
            ):
                finding(
                    "PROGRESS_EVIDENCE_UNAVAILABLE",
                    ("profile", "policy", "progress_revalidation_enabled"),
                    "Continuous revalidation needs complete incremental evidence and its read endpoint.",
                )
            automatic = (
                declared.read_snapshot
                and declared.snapshot_consistency != "UNVERIFIED"
                and declared.accept_plan
                and declared.query_action
                and declared.idempotency
                and declared.conditional_acceptance
                and {"accept_plan", "query_action"} <= endpoints
            )
            execution_mode = "CONDITIONAL" if automatic else "EXPORT_ONLY"
            if not automatic:
                finding(
                    "EXECUTION_EXPORT_ONLY",
                    ("mapping", "endpoint_bindings"),
                    "Execution capabilities are insufficient; plans can only be exported and imported after manual review.",
                    "WARNING",
                )
        if draft.policy.notification_channel == "workbench_and_email":
            finding(
                "EMAIL_CHANNEL_CHECK_REQUIRED",
                ("policy", "notification_channel"),
                "The mail channel and authorized test recipients must be verified separately; the configuration grants no sending right.",
                "WARNING",
            )
        finding(
            "ADMINISTRATOR_CONFIRMATION_REQUIRED",
            ("state",),
            "The check result awaits administrator confirmation in the sign-in service; not activated yet.",
            "INFO",
        )
    return OnboardingValidation(
        state="BLOCKED"
        if any(item.severity == "ERROR" for item in findings)
        else "NEEDS_CONFIRMATION",
        draft_hash=canonical_hash(draft) if draft is not None else None,
        snapshot_hash=snapshot.content_hash if snapshot is not None else None,
        execution_mode=execution_mode,
        findings=tuple(findings),
    )
