"""Immutable evidence of current validity; original candidate and approvals remain unchanged."""

from datetime import timedelta
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from packages.domain.models import (
    CheckReport,
    Contract,
    Digest,
    ErrorCode,
    Identifier,
    Metric,
    Timestamp,
    VersionBinding,
    canonical_hash,
    reject,
)
from packages.domain.objectives import METRIC_NAMES, METRIC_UNITS


class ValidationCertificate(Contract):
    schema_version: Literal["byof.validation-certificate/1"] = "byof.validation-certificate/1"
    certificate_id: Identifier
    factory_id: Identifier
    run_id: Identifier
    candidate_hash: Digest
    approval_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    original_binding: VersionBinding
    old_snapshot_id: Identifier
    old_snapshot_hash: Digest
    old_source_revision: Identifier
    new_snapshot_id: Identifier
    new_snapshot_hash: Digest
    new_source_revision: Identifier
    baseline_plan_version: Identifier
    baseline_plan_hash: Digest
    remaining_plan_hash: Digest
    source_evidence_hash: Digest
    checker: CheckReport
    metrics: tuple[Metric, ...]
    issued_at: Timestamp
    expires_at: Timestamp
    business_expires_at: Timestamp
    content_hash: Digest | None = None

    @model_validator(mode="after")
    def evidence_binding(self) -> Self:
        if len(set(self.approval_ids)) != len(self.approval_ids):
            reject(ErrorCode.DUPLICATE_ID, "Certificate approval references must be distinct")
        if (
            self.original_binding.snapshot_hash != self.old_snapshot_hash
            or self.original_binding.baseline_plan_version != self.baseline_plan_version
        ):
            reject(
                ErrorCode.VERSION_MISMATCH, "Certificate differs from the original approval binding"
            )
        revisions = (self.old_source_revision, self.new_source_revision)
        if any(
            not value.isascii() or not value.isdecimal() or str(int(value)) != value
            for value in revisions
        ):
            reject(
                ErrorCode.INVALID_INPUT, "Source revisions must be canonical nonnegative integers"
            )
        if (
            int(self.new_source_revision) <= int(self.old_source_revision)
            or self.old_snapshot_id == self.new_snapshot_id
            or self.old_snapshot_hash == self.new_snapshot_hash
        ):
            reject(
                ErrorCode.VERSION_MISMATCH, "Revalidation must identify strictly newer source facts"
            )
        if self.checker.status != "PASS" or self.checker.snapshot_hash != self.new_snapshot_hash:
            reject(
                ErrorCode.INVALID_INPUT,
                "Certificate requires a passing check of the current snapshot",
            )
        if len(self.metrics) != len(METRIC_NAMES) or {
            metric.name for metric in self.metrics
        } != set(METRIC_NAMES):
            reject(
                ErrorCode.INVALID_INPUT,
                "Certificate needs each current business metric exactly once",
            )
        units = {str(name): unit for name, unit in METRIC_UNITS.items()}
        for metric in self.metrics:
            if (
                metric.value is None
                or metric.lower_bound is not None
                or metric.unknown_reason is not None
                or metric.unit != units[metric.name]
            ):
                reject(
                    ErrorCode.INVALID_INPUT,
                    "Current metrics have known units and no inherited solver proof",
                )
            if metric.name != "incremental_overtime_metric" and metric.value < 0:
                reject(ErrorCode.INVALID_INPUT, "Only incremental overtime can be negative")
        if not self.issued_at < self.expires_at <= self.issued_at + timedelta(seconds=60):
            reject(
                ErrorCode.INVALID_TIME,
                "Certificate lifetime uses real time and cannot exceed sixty seconds",
            )
        digest = canonical_hash(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.content_hash is not None and self.content_hash != digest:
            reject(ErrorCode.HASH_MISMATCH, "Certificate content differs from its recorded hash")
        object.__setattr__(self, "content_hash", digest)
        return self
