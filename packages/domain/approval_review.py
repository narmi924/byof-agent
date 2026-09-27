"""A new human decision records the latest facts used to review unchanged future work."""

from typing import Literal, Self

from pydantic import model_validator

from packages.domain.models import (
    CheckReport,
    Contract,
    Digest,
    Identifier,
    Metric,
    Timestamp,
    VersionBinding,
    canonical_hash,
)
from packages.domain.objectives import METRIC_NAMES, METRIC_UNITS


class ApprovalReview(Contract):
    schema_version: Literal["byof.approval-review/1"] = "byof.approval-review/1"
    review_id: Identifier
    factory_id: Identifier
    run_id: Identifier
    candidate_id: Identifier
    candidate_hash: Digest
    approval_id: Identifier
    approver_id: Identifier
    approver_role: Literal["planner", "manager"]
    action_scope: Literal["publish_plan", "allow_overtime"]
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
    reviewed_at: Timestamp
    content_hash: Digest | None = None

    @model_validator(mode="after")
    def reviewed_facts(self) -> Self:
        if self.approver_role != (
            "manager" if self.action_scope == "allow_overtime" else "planner"
        ):
            raise ValueError("Review scope requires the corresponding human role")
        if (
            self.original_binding.snapshot_hash != self.old_snapshot_hash
            or self.original_binding.baseline_plan_version != self.baseline_plan_version
            or self.old_snapshot_id == self.new_snapshot_id
            or self.old_snapshot_hash == self.new_snapshot_hash
        ):
            raise ValueError("Review must retain original intent and identify new facts")
        revisions = (self.old_source_revision, self.new_source_revision)
        units = {str(name): unit for name, unit in METRIC_UNITS.items()}
        if any(
            not value.isascii() or not value.isdecimal() or str(int(value)) != value
            for value in revisions
        ) or int(self.new_source_revision) <= int(self.old_source_revision):
            raise ValueError("Review source revisions must strictly advance")
        if self.checker.status != "PASS" or self.checker.snapshot_hash != self.new_snapshot_hash:
            raise ValueError("Review requires a complete check of the latest facts")
        if len(self.metrics) != len(METRIC_NAMES) or {m.name for m in self.metrics} != set(
            METRIC_NAMES
        ):
            raise ValueError("Review requires all current business metrics")
        if any(
            m.value is None
            or m.lower_bound is not None
            or m.unknown_reason is not None
            or m.unit != units[m.name]
            or (m.name != "incremental_overtime_metric" and m.value < 0)
            for m in self.metrics
        ):
            raise ValueError("Review metrics cannot inherit an earlier optimization proof")
        digest = canonical_hash(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.content_hash is not None and self.content_hash != digest:
            raise ValueError("Review content hash differs from the saved human evidence")
        object.__setattr__(self, "content_hash", digest)
        return self
