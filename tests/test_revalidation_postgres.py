"""Original human approvals survive only certified normal progress over PostgreSQL and HTTP."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import approve_and_commit
from test_publication_postgres import publishing as publishing

from packages.auth import AccessError
from packages.domain.execution import PlanSubmission
from packages.domain.models import Candidate
from packages.domain.revalidation import ValidationCertificate
from packages.integrations.factory_http import ConnectorError
from packages.persistence import Membership, connect
from packages.planning.publication import Publication, commit_publication, deliver_one, publications
from packages.planning.revalidation import issue_certificate
from packages.planning.revalidation_store import ValidationRecord
from packages.planning.service import approve, request_solve, synchronize, workspace
from packages.planning.store import CandidateRecord, SolveJob
from services.factory_sim.storage import SourceAction, SourceChange
from services.solver_worker.main import run_once

pytestmark = pytest.mark.parametrize(
    "dynamic_source", [{"progress_revalidation": True}], indirect=True
)


@pytest.fixture
def progress(publishing):
    source, reader, writer, actor, *_ = publishing
    engine, factory = source[3], source[2].factory_id
    approve_and_commit(publishing)
    assert deliver_one(engine, reader, writer)
    original = synchronize(engine, reader, factory)
    job = request_solve(
        engine, actor, factory, request_id="remaining-solve", allow_overtime=False, time_limit=2
    )
    assert run_once(engine)
    with Session(engine) as db:
        completed = db.get(SolveJob, job.job_id)
        assert completed.state == "SUCCEEDED"
        candidate = Candidate.model_validate(
            db.get(CandidateRecord, completed.candidate_id).document
        )
    approved = approve(
        engine,
        actor,
        factory,
        candidate.candidate_id,
        request_id="remaining-approval",
        candidate_hash=candidate.content_hash,
        action_scope="publish_plan",
        decision="APPROVED",
    )
    assert candidate.accept_before > original.snapshot_clock + timedelta(minutes=15)
    assert control(source, "normal-prefix", "clock.step", {"minutes": 3}).status_code == 200
    current = synchronize(engine, reader, factory)
    assert current.actuals and current.source.source_revision != original.source.source_revision
    ctx = SimpleNamespace(
        source=source,
        reader=reader,
        writer=writer,
        actor=actor,
        engine=engine,
        factory=factory,
        original=original,
        candidate=candidate,
        approval=approved,
        current=current,
    )
    try:
        yield ctx
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        with owner.begin() as db:
            db.execute(delete(ValidationRecord).where(ValidationRecord.factory_id == factory))
        owner.dispose()


def certificate(ctx, request_id="validate"):
    return issue_certificate(
        ctx.engine,
        ctx.actor,
        ctx.factory,
        ctx.candidate.candidate_id,
        request_id=request_id,
        candidate_hash=ctx.candidate.content_hash,
    )


def commit(ctx, cert, request_id="publish-remaining"):
    return commit_publication(
        ctx.engine,
        ctx.actor,
        ctx.factory,
        ctx.candidate.candidate_id,
        request_id=request_id,
        candidate_hash=ctx.candidate.content_hash,
        certificate_id=cert.certificate_id,
    )


def submissions(ctx):
    with Session(ctx.source[4]) as db:
        return db.scalar(
            select(func.count())
            .select_from(SourceAction)
            .where(SourceAction.factory_id == ctx.factory, SourceAction.kind == "plan.submit")
        )


def test_normal_progress_certificate_preserves_approval_and_never_reconsumes_material(progress):
    ctx = progress
    assert workspace(ctx.engine, ctx.factory)["candidates"][0]["state"] == "STALE"
    with pytest.raises(AccessError):
        commit_publication(
            ctx.engine,
            ctx.actor,
            ctx.factory,
            ctx.candidate.candidate_id,
            request_id="strict-reject",
            candidate_hash=ctx.candidate.content_hash,
        )
    cert = certificate(ctx)
    assert cert.approval_ids == (ctx.approval.approval_id,)
    assert (
        cert.old_snapshot_hash == ctx.original.content_hash
        and cert.new_snapshot_hash == ctx.current.content_hash
    )
    assert cert.original_binding == ctx.candidate.binding and cert.checker.status == "PASS"
    assert all(metric.lower_bound is None for metric in cert.metrics)
    assert certificate(ctx) == cert
    with Session(ctx.engine) as db:
        assert db.get(
            CandidateRecord, ctx.candidate.candidate_id
        ).document == ctx.candidate.model_dump(mode="json")
    before = snapshot(ctx.source)
    release = commit(ctx, cert)
    with Session(ctx.engine) as db:
        payload = PlanSubmission.model_validate(db.get(Publication, release.release_id).payload)
    assert payload.certificate == cert and payload.candidate == ctx.candidate
    assert payload.approvals == (ctx.approval,)
    assert deliver_one(ctx.engine, ctx.reader, ctx.writer)
    after = snapshot(ctx.source)
    assert after.active_plan_hash == ctx.candidate.content_hash
    assert after.inventory == before.inventory and after.reservations == before.reservations
    assert after.actuals == before.actuals and submissions(ctx) == 2
    assert publications(ctx.engine, ctx.factory)[0]["release"].source_state == "ACTIVE"
    assert control(ctx.source, "continue-normal-work", "clock.step").status_code == 200
    progressed = snapshot(ctx.source)
    assert progressed.inventory == after.inventory
    assert progressed.actuals[0].remaining_minutes < after.actuals[0].remaining_minutes
    assert sum(len(a.consumed) for a in progressed.actuals) == sum(
        len(a.consumed) for a in after.actuals
    )


def test_certificate_request_is_concurrently_idempotent_and_database_record_immutable(progress):
    ctx = progress
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: certificate(ctx), range(2)))
    assert results[0] == results[1]
    with Session(ctx.engine) as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(ValidationRecord).where(ValidationRecord.factory_id == ctx.factory)
                    )
                )
            )
            == 1
        )
    with pytest.raises(ProgrammingError):
        with ctx.engine.begin() as db:
            db.execute(
                update(ValidationRecord)
                .where(ValidationRecord.certificate_id == results[0].certificate_id)
                .values(document=results[0].model_dump(mode="json"))
            )


def test_certificate_is_not_valid_after_more_progress_and_does_not_send(progress):
    ctx = progress
    cert = certificate(ctx)
    release = commit(ctx, cert)
    assert control(ctx.source, "advance-after-certificate", "clock.step").status_code == 200
    synchronize(ctx.engine, ctx.reader, ctx.factory)
    assert deliver_one(ctx.engine, ctx.reader, ctx.writer)
    result = next(
        p
        for p in publications(ctx.engine, ctx.factory)
        if p["release"].release_id == release.release_id
    )
    assert result["release"].source_state == "REJECTED" and submissions(ctx) == 1
    assert snapshot(ctx.source).active_plan_hash != ctx.candidate.content_hash


def test_source_checks_current_revision_even_without_byof_sync(progress):
    ctx = progress
    cert = certificate(ctx)
    commit(ctx, cert)
    assert control(ctx.source, "source-race", "clock.step").status_code == 200
    assert deliver_one(ctx.engine, ctx.reader, ctx.writer)
    assert publications(ctx.engine, ctx.factory)[0]["error_code"] == "SOURCE_CONDITIONS_CHANGED"
    assert (
        snapshot(ctx.source).active_plan_hash != ctx.candidate.content_hash
        and submissions(ctx) == 2
    )


@pytest.mark.parametrize("restore", [False, True])
def test_fault_mixed_into_normal_progress_rejects_even_after_restoration(progress, restore):
    ctx = progress
    resource_id = ctx.current.resources[-1].resource_id
    assert (
        control(ctx.source, "fault", "resource.down", {"resource_id": resource_id}).status_code
        == 200
    )
    if restore:
        assert (
            control(
                ctx.source, "restore", "resource.restore", {"resource_id": resource_id}
            ).status_code
            == 200
        )
    synchronize(ctx.engine, ctx.reader, ctx.factory)
    with pytest.raises(AccessError):
        certificate(ctx)
    assert submissions(ctx) == 1
    with Session(ctx.engine) as db:
        assert (
            db.scalar(select(ValidationRecord).where(ValidationRecord.factory_id == ctx.factory))
            is None
        )


def test_revoked_planner_cannot_issue_or_use_a_certificate(progress):
    ctx = progress
    cert = certificate(ctx)
    with ctx.engine.begin() as db:
        db.execute(
            delete(Membership).where(
                Membership.user_id == ctx.actor.user_id, Membership.role == "planner"
            )
        )
    with pytest.raises(AccessError):
        certificate(ctx)
    with pytest.raises(AccessError):
        commit(ctx, cert)
    assert submissions(ctx) == 1


def test_original_approval_can_be_revoked_after_progress_and_invalidates_certificate(progress):
    ctx = progress
    cert = certificate(ctx)
    rejected = approve(
        ctx.engine,
        ctx.actor,
        ctx.factory,
        ctx.candidate.candidate_id,
        request_id="withdraw-original",
        candidate_hash=ctx.candidate.content_hash,
        action_scope="publish_plan",
        decision="REJECTED",
    )
    assert rejected.binding == ctx.approval.binding and rejected.decision == "REJECTED"
    with pytest.raises(AccessError):
        commit(ctx, cert)
    with pytest.raises(AccessError):
        approve(
            ctx.engine,
            ctx.actor,
            ctx.factory,
            ctx.candidate.candidate_id,
            request_id="cannot-reapprove-old",
            candidate_hash=ctx.candidate.content_hash,
            action_scope="publish_plan",
            decision="APPROVED",
        )
    assert submissions(ctx) == 1


def test_lost_source_receipt_uses_original_certificate_and_one_external_action(progress):
    ctx = progress
    cert = certificate(ctx)
    release = commit(ctx, cert)

    class LostReceipt:
        calls = 0

        def submit(self, payload):
            self.calls += 1
            ctx.writer.submit(payload)
            raise ConnectorError("Accepted but receipt lost")

    writer = LostReceipt()
    assert deliver_one(ctx.engine, ctx.reader, writer)
    assert publications(ctx.engine, ctx.factory)[0]["release"].source_state == "UNKNOWN"
    with ctx.engine.begin() as db:
        db.execute(
            update(Publication)
            .where(Publication.release_id == release.release_id)
            .values(next_attempt_at=datetime.now(UTC))
        )
    assert deliver_one(ctx.engine, ctx.reader, writer)
    assert writer.calls == 1 and submissions(ctx) == 2
    assert publications(ctx.engine, ctx.factory)[0]["release"].source_state == "ACTIVE"
    recovered = commit(ctx, cert)
    assert recovered.source_state == "ACTIVE"
    assert recovered.operation_id == release.operation_id
    assert recovered.payload_hash == release.payload_hash
    assert recovered.approval_ids == release.approval_ids
    with Session(ctx.engine) as db:
        assert (
            PlanSubmission.model_validate(
                db.get(Publication, release.release_id).payload
            ).certificate
            == cert
        )


@pytest.mark.parametrize("corruption", ["metrics", "approval", "expired", "scope"])
def test_source_independently_rejects_forged_or_expired_certificate(progress, corruption):
    ctx = progress
    cert = certificate(ctx)
    release = commit(ctx, cert)
    with Session(ctx.engine) as db:
        payload = PlanSubmission.model_validate(db.get(Publication, release.release_id).payload)
    raw = cert.model_dump(mode="json", exclude={"content_hash"})
    if corruption == "metrics":
        raw["metrics"][0]["value"] += 1
    elif corruption == "approval":
        raw["approval_ids"] = ["not-the-human-approval"]
    elif corruption == "expired":
        raw["issued_at"] = (datetime.now(UTC) - timedelta(seconds=59)).isoformat()
        raw["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    else:
        raw["factory_id"] = "another-factory"
    changed = ValidationCertificate.model_validate(raw)
    data = payload.model_dump(mode="json")
    data["operation_id"] = str(uuid4())
    data["certificate"] = changed.model_dump(mode="json")
    before = snapshot(ctx.source)
    receipt = ctx.writer.submit(PlanSubmission.model_validate(data))
    assert receipt.source_state == "REJECTED"
    after = snapshot(ctx.source)
    assert after.content_hash == before.content_hash and after.inventory == before.inventory
    assert after.active_plan_hash != ctx.candidate.content_hash


@pytest.mark.parametrize("deadline", ["certificate", "approval", "after-verification"])
def test_source_rechecks_real_deadlines_after_expensive_verification(
    progress, monkeypatch, deadline
):
    from packages.planning import revalidation_check
    from services.factory_sim import revalidation as source_check
    from services.factory_sim import service as source_service

    ctx = progress
    cert = certificate(ctx)
    release = commit(ctx, cert)
    with Session(ctx.engine) as db:
        payload = PlanSubmission.model_validate(db.get(Publication, release.release_id).payload)
    start = datetime.now(UTC)
    expires = cert.expires_at
    if deadline == "approval":
        data = payload.model_dump(mode="json")
        expires = start + timedelta(seconds=1)
        data["approvals"][0]["expires_at"] = expires.isoformat()
        payload = PlanSubmission.model_validate(data)

    class Clock:
        value = start

        @classmethod
        def now(cls, zone):
            return cls.value

    monkeypatch.setattr(source_service, "datetime", Clock)
    monkeypatch.setattr(revalidation_check, "datetime", Clock)
    if deadline == "after-verification":
        original = source_check.check_source_certificate

        def slow_verification(*args, **kwargs):
            original(*args, **kwargs)
            Clock.value = expires

        monkeypatch.setattr(source_check, "check_source_certificate", slow_verification)
    else:
        checker = revalidation_check.check_revalidated_plan

        def slow_checker(*args, **kwargs):
            result = checker(*args, **kwargs)
            Clock.value = expires
            return result

        monkeypatch.setattr(revalidation_check, "check_revalidated_plan", slow_checker)
    before = snapshot(ctx.source)
    receipt = ctx.writer.submit(payload)
    assert receipt.source_state == "REJECTED"
    assert receipt.error_code == (
        "APPROVAL_REQUIRED" if deadline == "approval" else "VALIDATION_EXPIRED"
    )
    assert receipt.recorded_at == expires
    assert snapshot(ctx.source) == before
    with Session(ctx.source[4]) as db:
        activations = db.scalars(
            select(SourceChange).where(
                SourceChange.factory_id == ctx.factory,
                SourceChange.document["cause"].astext == "plan.activated",
            )
        ).all()
    assert len(activations) == 1 and submissions(ctx) == 2


def test_unknown_cannot_be_replaced_by_new_request_or_certificate(progress):
    ctx = progress
    cert = certificate(ctx)
    release = commit(ctx, cert)

    class LostReceipt:
        calls = 0

        def submit(self, payload):
            self.calls += 1
            ctx.writer.submit(payload)
            raise ConnectorError("Source accepted; original response lost")

    writer = LostReceipt()
    assert deliver_one(ctx.engine, ctx.reader, writer)
    assert commit(ctx, cert).source_state == "UNKNOWN"
    replacement = certificate(ctx, "new-validation-request")
    assert replacement.certificate_id != cert.certificate_id
    with pytest.raises(AccessError) as failure:
        commit(ctx, replacement, "new-publication-request")
    assert failure.value.code == "UNRESOLVED_PUBLICATION"
    assert release.release_id in failure.value.message
    with Session(ctx.engine) as db:
        pending = db.scalars(
            select(Publication).where(
                Publication.factory_id == ctx.factory,
                Publication.candidate_id == ctx.candidate.candidate_id,
            )
        ).all()
        assert len(pending) == 1 and pending[0].state == "UNKNOWN"
        assert PlanSubmission.model_validate(pending[0].payload).certificate == cert
    with ctx.engine.begin() as db:
        db.execute(
            update(Publication)
            .where(Publication.release_id == release.release_id)
            .values(next_attempt_at=datetime.now(UTC))
        )
    assert deliver_one(ctx.engine, ctx.reader, writer)
    assert commit(ctx, cert).source_state == "ACTIVE"
    assert writer.calls == 1 and submissions(ctx) == 2


@pytest.mark.parametrize("state", ["QUEUED", "DELIVERING"])
def test_unresolved_publication_fence_also_covers_queued_and_crashed_worker(progress, state):
    ctx = progress
    cert = certificate(ctx)
    release = commit(ctx, cert)
    with ctx.engine.begin() as db:
        db.execute(
            update(Publication)
            .where(Publication.release_id == release.release_id)
            .values(state=state, attempts=int(state == "DELIVERING"))
        )
    with pytest.raises(AccessError) as failure:
        commit(ctx, cert, "replacement")
    assert failure.value.code == "UNRESOLVED_PUBLICATION"
    assert commit(ctx, cert).operation_id == release.operation_id
    assert submissions(ctx) == 1


def test_approval_expiring_during_certificate_issuance_leaves_no_certificate(progress, monkeypatch):
    from packages.planning import publication, revalidation

    ctx = progress
    checker = revalidation.check_revalidated_plan

    class Clock:
        value = datetime.now(UTC)

        @classmethod
        def now(cls, zone):
            return cls.value

    def slow_checker(*args, **kwargs):
        result = checker(*args, **kwargs)
        Clock.value = ctx.approval.expires_at
        return result

    monkeypatch.setattr(revalidation, "datetime", Clock)
    monkeypatch.setattr(publication, "datetime", Clock)
    monkeypatch.setattr(revalidation, "check_revalidated_plan", slow_checker)
    with pytest.raises(AccessError) as failure:
        certificate(ctx)
    assert failure.value.code == "APPROVAL_REQUIRED"
    with Session(ctx.engine) as db:
        assert (
            db.scalar(select(ValidationRecord).where(ValidationRecord.factory_id == ctx.factory))
            is None
        )
    assert submissions(ctx) == 1
