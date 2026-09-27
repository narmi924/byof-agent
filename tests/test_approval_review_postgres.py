"""First human approval records current evidence while the independent factory keeps running."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import approve_and_commit
from test_publication_postgres import publishing as publishing

from packages.auth import AccessError
from packages.domain.approval_review import ApprovalReview
from packages.domain.models import Candidate
from packages.persistence import Membership, connect
from packages.planning.publication import commit_publication, deliver_one, publications
from packages.planning.revalidation import issue_certificate
from packages.planning.revalidation_store import ValidationRecord
from packages.planning.review_store import ApprovalReviewRecord
from packages.planning.reviews import approve_progress
from packages.planning.service import approve, request_solve, synchronize
from packages.planning.store import ApprovalRecord, CandidateRecord, SolveJob
from services.factory_sim.service import run_due_tick
from services.factory_sim.storage import World
from services.solver_worker.main import run_once

pytestmark = pytest.mark.parametrize(
    "dynamic_source", [{"progress_revalidation": True}], indirect=True
)


@pytest.fixture
def unreviewed(publishing):
    source, reader, writer, actor, *_ = publishing
    engine, factory = source[3], source[2].factory_id
    approve_and_commit(publishing)
    assert deliver_one(engine, reader, writer)
    original = synchronize(engine, reader, factory)
    job = request_solve(
        engine, actor, factory, request_id="unreviewed", allow_overtime=False, time_limit=2
    )
    assert run_once(engine)
    with Session(engine) as db:
        solved = db.get(SolveJob, job.job_id)
        assert solved.state == "SUCCEEDED"
        candidate = Candidate.model_validate(db.get(CandidateRecord, solved.candidate_id).document)
        assert (
            db.scalar(
                select(ApprovalRecord).where(ApprovalRecord.candidate_id == candidate.candidate_id)
            )
            is None
        )
    ctx = SimpleNamespace(
        source=source,
        engine=engine,
        factory=factory,
        reader=reader,
        writer=writer,
        actor=actor,
        original=original,
        candidate=candidate,
    )
    try:
        yield ctx
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        with owner.begin() as db:
            db.execute(
                delete(ApprovalReviewRecord).where(ApprovalReviewRecord.factory_id == factory)
            )
            db.execute(delete(ValidationRecord).where(ValidationRecord.factory_id == factory))
        owner.dispose()


def review(ctx, request_id="first-human-review"):
    return approve_progress(
        ctx.engine,
        ctx.actor,
        ctx.factory,
        ctx.candidate.candidate_id,
        request_id=request_id,
        candidate_hash=ctx.candidate.content_hash,
        action_scope="publish_plan",
        decision="APPROVED",
    )


def tick(ctx):
    assert (
        control(ctx.source, "before-human-review", "clock.step", {"minutes": 3}).status_code == 200
    )
    return synchronize(ctx.engine, ctx.reader, ctx.factory)


def rows(ctx):
    with Session(ctx.engine) as db:
        return list(
            db.scalars(
                select(ApprovalReviewRecord).where(ApprovalReviewRecord.factory_id == ctx.factory)
            )
        )


def test_first_approval_has_new_human_evidence_and_can_be_certified(unreviewed):
    ctx = unreviewed
    current = tick(ctx)
    with pytest.raises(AccessError):
        approve(
            ctx.engine,
            ctx.actor,
            ctx.factory,
            ctx.candidate.candidate_id,
            request_id="strict",
            candidate_hash=ctx.candidate.content_hash,
            action_scope="publish_plan",
            decision="APPROVED",
        )
    approved = review(ctx)
    saved = ApprovalReview.model_validate(rows(ctx)[0].document)
    assert saved.approval_id == approved.approval_id and saved.approver_id == ctx.actor.user_id
    assert saved.old_snapshot_hash == ctx.original.content_hash
    assert saved.new_snapshot_hash == current.content_hash
    assert saved.reviewed_at == approved.decided_at and saved.checker.status == "PASS"
    assert all(m.lower_bound is None for m in saved.metrics)
    cert = issue_certificate(
        ctx.engine,
        ctx.actor,
        ctx.factory,
        ctx.candidate.candidate_id,
        request_id="post-review-certificate",
        candidate_hash=ctx.candidate.content_hash,
    )
    assert cert.approval_ids == (approved.approval_id,)
    commit_publication(
        ctx.engine,
        ctx.actor,
        ctx.factory,
        ctx.candidate.candidate_id,
        request_id="post-review-publication",
        candidate_hash=ctx.candidate.content_hash,
        certificate_id=cert.certificate_id,
    )
    assert deliver_one(ctx.engine, ctx.reader, ctx.writer)
    assert publications(ctx.engine, ctx.factory)[0]["release"].source_state == "ACTIVE"


def test_first_human_review_while_real_independent_clock_keeps_running(unreviewed):
    ctx = unreviewed
    assert (
        control(
            ctx.source, "run-through-human-review", "clock.run", {"interval_ms": 200}
        ).status_code
        == 200
    )
    stop = Event()
    errors = []

    def clock_loop():
        try:
            while not stop.wait(0.005):
                run_due_tick(ctx.source[4])
        except Exception as exc:
            errors.append(type(exc).__name__)

    worker = Thread(target=clock_loop)
    worker.start()
    try:
        deadline = monotonic() + 5
        while snapshot(ctx.source).snapshot_clock <= ctx.original.snapshot_clock:
            assert monotonic() < deadline and not errors
            stop.wait(0.02)
        current = synchronize(ctx.engine, ctx.reader, ctx.factory)
        approved = review(ctx)
        assert approved.decision == "APPROVED"
        saved = ApprovalReview.model_validate(rows(ctx)[0].document)
        assert saved.new_snapshot_hash == current.content_hash
        assert saved.old_source_revision != saved.new_source_revision
        while snapshot(ctx.source).snapshot_clock <= current.snapshot_clock:
            assert monotonic() < deadline and not errors
            stop.wait(0.02)
        with Session(ctx.source[4]) as db:
            assert db.get(World, ctx.factory).mode == "RUNNING"
        assert not errors and worker.is_alive()
    finally:
        stop.set()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_review_is_idempotent_immutable_and_never_revives_withdrawn_approval(unreviewed):
    ctx = unreviewed
    tick(ctx)
    with ThreadPoolExecutor(max_workers=2) as pool:
        approved = list(pool.map(lambda _: review(ctx), range(2)))
    assert approved[0] == approved[1] and len(rows(ctx)) == 1
    with pytest.raises(ProgrammingError):
        with ctx.engine.begin() as db:
            db.execute(
                update(ApprovalReviewRecord)
                .where(ApprovalReviewRecord.factory_id == ctx.factory)
                .values(document={})
            )
    rejected = approve(
        ctx.engine,
        ctx.actor,
        ctx.factory,
        ctx.candidate.candidate_id,
        request_id="withdraw-reviewed",
        candidate_hash=ctx.candidate.content_hash,
        action_scope="publish_plan",
        decision="REJECTED",
    )
    assert rejected.decision == "REJECTED" and review(ctx) == approved[0]
    with pytest.raises(AccessError):
        issue_certificate(
            ctx.engine,
            ctx.actor,
            ctx.factory,
            ctx.candidate.candidate_id,
            request_id="withdrawn",
            candidate_hash=ctx.candidate.content_hash,
        )


def test_strict_request_cannot_be_reused_in_checked_review_mode(unreviewed):
    ctx = unreviewed
    approve(
        ctx.engine,
        ctx.actor,
        ctx.factory,
        ctx.candidate.candidate_id,
        request_id="mode-conflict",
        candidate_hash=ctx.candidate.content_hash,
        action_scope="publish_plan",
        decision="APPROVED",
    )
    tick(ctx)
    with pytest.raises(AccessError) as error:
        review(ctx, "mode-conflict")
    assert error.value.code == "IDEMPOTENCY_CONFLICT" and rows(ctx) == []


def test_checked_request_cannot_be_reused_as_a_strict_approval(unreviewed):
    ctx = unreviewed
    tick(ctx)
    review(ctx)
    with pytest.raises(AccessError) as error:
        approve(
            ctx.engine,
            ctx.actor,
            ctx.factory,
            ctx.candidate.candidate_id,
            request_id="first-human-review",
            candidate_hash=ctx.candidate.content_hash,
            action_scope="publish_plan",
            decision="APPROVED",
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_failure_after_both_review_and_approval_inserts_rolls_back_both(unreviewed, monkeypatch):
    from packages.agent import human_tasks

    ctx = unreviewed
    tick(ctx)
    reached = []

    def interrupted(db, current):
        assert (
            db.scalar(
                select(ApprovalRecord).where(
                    ApprovalRecord.candidate_id == ctx.candidate.candidate_id
                )
            )
            is not None
        )
        assert (
            db.scalar(
                select(ApprovalReviewRecord).where(
                    ApprovalReviewRecord.candidate_id == ctx.candidate.candidate_id
                )
            )
            is not None
        )
        reached.append(current.snapshot_id)
        raise RuntimeError("Interrupted after both inserts before commit")

    monkeypatch.setattr(human_tasks, "reconcile_reviews", interrupted)
    before = snapshot(ctx.source)
    with pytest.raises(RuntimeError, match="after both inserts"):
        review(ctx)
    assert len(reached) == 1 and rows(ctx) == []
    assert snapshot(ctx.source) == before
    with Session(ctx.engine) as db:
        assert (
            db.scalar(
                select(ApprovalRecord).where(
                    ApprovalRecord.candidate_id == ctx.candidate.candidate_id
                )
            )
            is None
        )


@pytest.mark.parametrize("corruption", ["fault", "fault-restored", "expired", "revoked", "hash"])
def test_first_review_rejects_changes_without_granting_approval(unreviewed, corruption):
    ctx = unreviewed
    tick(ctx)
    if corruption in {"fault", "fault-restored"}:
        resource_id = ctx.original.resources[-1].resource_id
        assert (
            control(
                ctx.source, "review-fault", "resource.down", {"resource_id": resource_id}
            ).status_code
            == 200
        )
        if corruption == "fault-restored":
            assert (
                control(
                    ctx.source, "review-restore", "resource.restore", {"resource_id": resource_id}
                ).status_code
                == 200
            )
        synchronize(ctx.engine, ctx.reader, ctx.factory)
    elif corruption == "expired":
        from packages.planning.store import FactoryState

        with ctx.engine.begin() as db:
            db.execute(
                update(FactoryState)
                .where(FactoryState.factory_id == ctx.factory)
                .values(last_synced_at=datetime.now(UTC) - timedelta(seconds=31))
            )
    elif corruption == "revoked":
        with ctx.engine.begin() as db:
            db.execute(
                delete(Membership).where(
                    Membership.user_id == ctx.actor.user_id, Membership.role == "planner"
                )
            )
    else:
        ctx.candidate = ctx.candidate.model_copy(update={"content_hash": "0" * 64})
    with pytest.raises(AccessError):
        review(ctx)
    assert rows(ctx) == []
    with Session(ctx.engine) as db:
        assert (
            db.scalar(
                select(ApprovalRecord).where(
                    ApprovalRecord.candidate_id == ctx.candidate.candidate_id
                )
            )
            is None
        )
