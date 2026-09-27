"""Only a known source rejection permits a new, freshly certified publication operation."""

from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing
from test_revalidation_postgres import certificate, commit, submissions
from test_revalidation_postgres import progress as progress

from packages.auth import AccessError
from packages.domain.execution import PlanSubmission
from packages.integrations.factory_http import ConnectorError
from packages.planning.publication import Publication, deliver_one
from packages.planning.revalidation_store import ValidationRecord
from packages.planning.service import synchronize
from packages.planning.store import ApprovalRecord
from services.factory_sim.storage import SourceAction

pytestmark = pytest.mark.parametrize(
    "dynamic_source", [{"progress_revalidation": True}], indirect=True
)


def publication_record(ctx, release_id):
    with Session(ctx.engine) as db:
        row = db.get(Publication, release_id)
        assert row is not None
        return deepcopy(
            {
                "payload": row.payload,
                "document": row.document,
                "source_receipt": row.source_receipt,
                "state": row.state,
                "error_code": row.error_code,
                "attempts": row.attempts,
            }
        )


def candidate_publications(ctx):
    with Session(ctx.engine) as db:
        return list(
            db.scalars(
                select(Publication.release_id).where(
                    Publication.factory_id == ctx.factory,
                    Publication.candidate_id == ctx.candidate.candidate_id,
                )
            )
        )


def approvals(ctx):
    with Session(ctx.engine) as db:
        return {
            row.approval_id: deepcopy(row.document)
            for row in db.scalars(
                select(ApprovalRecord).where(ApprovalRecord.factory_id == ctx.factory)
            )
        }


def source_plans(ctx):
    with Session(ctx.source[4]) as db:
        return {
            row.operation_id: deepcopy(row.result)
            for row in db.scalars(
                select(SourceAction).where(
                    SourceAction.factory_id == ctx.factory, SourceAction.kind == "plan.submit"
                )
            )
        }


def rejected_by_progress(ctx):
    cert = certificate(ctx, "certificate-before-source-tick")
    release = commit(ctx, cert, "publication-before-source-tick")
    assert control(ctx.source, "source-tick-after-commit", "clock.step").status_code == 200
    progressed = snapshot(ctx.source)
    assert progressed.source.source_revision != cert.new_source_revision
    assert progressed.actuals != ctx.current.actuals
    # No synchronization here: rejection must come from the HTTP execution source.
    assert deliver_one(ctx.engine, ctx.reader, ctx.writer)
    record = publication_record(ctx, release.release_id)
    assert record["state"] == "DONE" and record["document"]["source_state"] == "REJECTED"
    assert record["error_code"] == "SOURCE_CONDITIONS_CHANGED"
    assert record["source_receipt"]["error_code"] == "SOURCE_CONDITIONS_CHANGED"
    assert record["source_receipt"] == source_plans(ctx)[release.operation_id]
    assert snapshot(ctx.source) == progressed
    assert submissions(ctx) == 2
    return cert, release, record, progressed


def test_known_progress_rejection_allows_new_certificate_without_replacing_original_approval(
    progress,
):
    ctx = progress
    original_approvals = approvals(ctx)
    old_certificate, old_release, rejected, progressed = rejected_by_progress(ctx)
    latest = synchronize(ctx.engine, ctx.reader, ctx.factory)
    assert latest.content_hash == progressed.content_hash
    fresh = certificate(ctx, "certificate-after-known-rejection")
    assert fresh.certificate_id != old_certificate.certificate_id
    assert fresh.approval_ids == old_certificate.approval_ids == (ctx.approval.approval_id,)
    assert fresh.original_binding == old_certificate.original_binding == ctx.candidate.binding
    assert fresh.new_snapshot_hash == latest.content_hash != old_certificate.new_snapshot_hash
    replacement = commit(ctx, fresh, "publication-after-known-rejection")
    assert replacement.release_id != old_release.release_id
    assert replacement.operation_id != old_release.operation_id
    assert replacement.payload_hash != old_release.payload_hash
    assert replacement.candidate_hash == old_release.candidate_hash == ctx.candidate.content_hash
    payload = PlanSubmission.model_validate(
        publication_record(ctx, replacement.release_id)["payload"]
    )
    assert payload.certificate == fresh and payload.approvals == (ctx.approval,)
    assert payload.candidate == ctx.candidate
    assert deliver_one(ctx.engine, ctx.reader, ctx.writer)
    accepted = publication_record(ctx, replacement.release_id)
    assert accepted["document"]["source_state"] == "ACTIVE"
    assert accepted["state"] == "DONE" and accepted["error_code"] is None
    assert publication_record(ctx, old_release.release_id) == rejected
    assert set(candidate_publications(ctx)) == {old_release.release_id, replacement.release_id}
    assert approvals(ctx) == original_approvals
    final = snapshot(ctx.source)
    assert final.active_plan_hash == ctx.candidate.content_hash
    assert final.inventory == progressed.inventory
    assert final.reservations == progressed.reservations and final.actuals == progressed.actuals
    plans = source_plans(ctx)
    assert len(plans) == submissions(ctx) == 3
    assert plans[old_release.operation_id]["source_state"] == "REJECTED"
    assert plans[replacement.operation_id]["source_state"] == "ACTIVE"
    assert sorted(row["source_state"] for row in plans.values()) == ["ACTIVE", "ACTIVE", "REJECTED"]


@pytest.mark.parametrize("kind", ["resource.down", "order.add"])
def test_material_change_after_known_rejection_prevents_another_certificate_or_publication(
    progress, kind
):
    ctx = progress
    original_approvals = approvals(ctx)
    _, release, rejected, before = rejected_by_progress(ctx)
    if kind == "resource.down":
        payload = {"resource_id": before.resources[-1].resource_id}
        expected = "UNRESOLVED_PROGRESS_FACT"
    else:
        payload = before.orders[0].model_dump(mode="json")
        payload.update(
            order_id="urgent-after-rejection", quantity=50, status="CONFIRMED", version=1
        )
        expected = "MATERIAL_PROGRESS_EVENT"
    assert control(ctx.source, "material-change-after-rejection", kind, payload).status_code == 200
    synchronize(ctx.engine, ctx.reader, ctx.factory)
    with pytest.raises(AccessError) as failure:
        certificate(ctx, "certificate-must-not-ignore-material-change")
    assert failure.value.code == expected
    assert candidate_publications(ctx) == [release.release_id]
    assert publication_record(ctx, release.release_id) == rejected
    assert approvals(ctx) == original_approvals and submissions(ctx) == 2
    with Session(ctx.engine) as db:
        certificates = list(
            db.scalars(select(ValidationRecord).where(ValidationRecord.factory_id == ctx.factory))
        )
    assert len(certificates) == 1
    assert (
        snapshot(ctx.source).active_plan_hash
        == before.active_plan_hash
        != ctx.candidate.content_hash
    )


def test_lost_rejection_receipt_remains_unknown_and_blocks_a_new_operation(progress):
    ctx = progress
    cert = certificate(ctx, "certificate-before-lost-rejection")
    release = commit(ctx, cert, "publication-before-lost-rejection")
    assert control(ctx.source, "normal-tick-before-lost-rejection", "clock.step").status_code == 200

    class LostRejection:
        calls = 0

        def submit(self, payload):
            self.calls += 1
            receipt = ctx.writer.submit(payload)
            assert receipt.source_state == "REJECTED"
            assert receipt.error_code == "SOURCE_CONDITIONS_CHANGED"
            raise ConnectorError("Source rejection receipt was lost")

    writer = LostRejection()
    assert deliver_one(ctx.engine, ctx.reader, writer)
    unknown = publication_record(ctx, release.release_id)
    assert unknown["state"] == unknown["document"]["source_state"] == "UNKNOWN"
    assert unknown["source_receipt"] is None and unknown["error_code"] == "SOURCE_RESULT_UNKNOWN"
    synchronize(ctx.engine, ctx.reader, ctx.factory)
    replacement = certificate(ctx, "fresh-certificate-with-unknown-publication")
    with pytest.raises(AccessError) as failure:
        commit(ctx, replacement, "forbidden-replacement-of-unknown")
    assert failure.value.code == "UNRESOLVED_PUBLICATION"
    assert release.release_id in failure.value.message
    assert publication_record(ctx, release.release_id) == unknown
    assert candidate_publications(ctx) == [release.release_id]
    assert source_plans(ctx)[release.operation_id]["source_state"] == "REJECTED"
    assert writer.calls == 1 and submissions(ctx) == 2
