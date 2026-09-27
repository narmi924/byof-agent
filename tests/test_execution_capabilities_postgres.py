"""Read-only factories retain planning and export, while publishing obeys actual source guarantees."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import approve_and_commit
from test_publication_postgres import publishing as publishing
from test_revalidation_postgres import progress as progress

from packages.auth import AccessError, Grant, Principal, hasher
from packages.domain.models import canonical_hash
from packages.integrations.factory_http import ConnectorError
from packages.persistence import LoginSession, User
from packages.planning import exports
from packages.planning.exports import export_candidate
from packages.planning.publication import Publication, commit_publication, deliver_one, publications
from packages.planning.service import approve, request_solve, synchronize, workspace
from packages.planning.store import ApprovalRecord, FactoryState, SolveJob
from packages.settings import Settings
from services.api.main import create_app
from services.factory_sim.storage import SourceAction
from services.solver_worker.main import run_once


def approve_only(context):
    source, _, _, actor, candidate_id, candidate_hash = context
    return approve(
        source[3],
        actor,
        source[2].factory_id,
        candidate_id,
        request_id="review-without-execution",
        candidate_hash=candidate_hash,
        action_scope="publish_plan",
        decision="APPROVED",
    )


def export(context, actor=None, candidate_hash=None):
    source, _, _, planner, candidate_id, digest = context
    return export_candidate(
        source[3],
        actor or planner,
        source[2].factory_id,
        candidate_id,
        candidate_hash=candidate_hash or digest,
    )


@pytest.mark.parametrize("dynamic_source", [{"read_only": True}], indirect=True)
def test_real_read_only_http_factory_can_solve_approve_export_with_zero_source_writes(publishing):
    source, reader, _, actor, candidate_id, digest = publishing
    engine, factory = source[3], source[2].factory_id
    before = snapshot(source)
    assert not reader.capabilities().conditional_acceptance
    approved = approve_only(publishing)
    with pytest.raises(AccessError) as failure:
        commit_publication(
            engine, actor, factory, candidate_id, request_id="unsafe-submit", candidate_hash=digest
        )
    assert failure.value.code == "EXECUTION_CAPABILITY_REQUIRED"
    job = request_solve(
        engine,
        actor,
        factory,
        request_id="manual-still-available",
        allow_overtime=False,
        time_limit=2,
    )
    assert run_once(engine)
    with Session(engine) as db:
        assert db.get(SolveJob, job.job_id).state == "SUCCEEDED"
        assert db.get(ApprovalRecord, approved.approval_id).document["decision"] == "APPROVED"
        assert db.scalar(select(Publication).where(Publication.factory_id == factory)) is None
    exported = export(publishing)
    assert (
        exported["purpose"] == "MANUAL_REVIEW" and exported["candidate"]["content_hash"] == digest
    )
    assert exported["original_checker"]["status"] == exported["current_checker"]["status"] == "PASS"
    assert exported["content_hash"] == canonical_hash(
        {k: v for k, v in exported.items() if k != "content_hash"}
    )
    assert workspace(engine, factory)["execution_support"]["mode"] == "EXPORT_ONLY"
    assert snapshot(source) == before
    with Session(source[4]) as db:
        assert db.scalar(select(SourceAction).where(SourceAction.factory_id == factory)) is None


@pytest.mark.parametrize("where", ["persisted", "live"])
def test_capability_loss_after_commit_prevents_first_send(publishing, where):
    source, reader, writer, _, _, _ = publishing
    engine, factory = source[3], source[2].factory_id
    _, release = approve_and_commit(publishing)
    unsupported = reader.capabilities().model_copy(update={"conditional_acceptance": False})
    if where == "persisted":
        with Session(engine) as db, db.begin():
            state = db.get(FactoryState, factory)
            state.connector_capabilities = unsupported.model_dump(mode="json")
            state.capabilities_observed_at = datetime.now(UTC)

    class Reader:
        def action(self, *args):
            return reader.action(*args)

        def capabilities(self):
            return unsupported if where == "live" else reader.capabilities()

    class Writer:
        calls = 0

        def submit(self, body):
            self.calls += 1
            return writer.submit(body)

    observed = Writer()
    assert deliver_one(engine, Reader(), observed)
    assert observed.calls == 0
    row = publications(engine, factory)[0]
    assert (
        row["release"].release_id == release.release_id
        and row["release"].source_state == "REJECTED"
    )
    assert row["error_code"] == "EXECUTION_CAPABILITY_REQUIRED"
    assert snapshot(source).active_plan_version is None


def test_lost_receipt_recovers_original_action_even_after_capability_loss(publishing):
    source, reader, writer, actor, candidate_id, digest = publishing
    engine, factory = source[3], source[2].factory_id
    _, release = approve_and_commit(publishing)

    class Lost:
        calls = 0

        def submit(self, body):
            self.calls += 1
            writer.submit(body)
            raise ConnectorError("lost receipt")

    lost = Lost()
    assert deliver_one(engine, reader, lost)
    with Session(engine) as db, db.begin():
        state = db.get(FactoryState, factory)
        state.connector_capabilities = None
        state.capabilities_observed_at = None
        db.get(Publication, release.release_id).next_attempt_at = datetime.now(UTC)
    assert (
        commit_publication(
            engine, actor, factory, candidate_id, request_id="publication", candidate_hash=digest
        ).release_id
        == release.release_id
    )
    assert deliver_one(engine, reader, lost)
    assert lost.calls == 1
    assert publications(engine, factory)[0]["release"].source_state == "ACTIVE"
    with Session(source[4]) as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(SourceAction).where(
                            SourceAction.factory_id == factory, SourceAction.kind == "plan.submit"
                        )
                    )
                )
            )
            == 1
        )


def test_export_rejects_cross_factory_role_or_changed_hash(publishing):
    source, _, _, actor, _, _ = publishing
    outsider = Principal(
        user_id=actor.user_id,
        username=actor.username,
        grants=(Grant(factory_id="other", role="planner"),),
    )
    with pytest.raises(AccessError) as failure:
        export(publishing, actor=outsider)
    assert failure.value.code == "FORBIDDEN"
    with pytest.raises(AccessError) as failure:
        export(publishing, candidate_hash="0" * 64)
    assert failure.value.code == "CANDIDATE_CHANGED"
    assert publications(source[3], source[2].factory_id) == []


def test_export_of_old_candidate_reports_unverified_current_facts_and_keeps_old_plan(publishing):
    source, reader, _, _, _, digest = publishing
    original = export(publishing)
    assert control(source, "new-business-clock", "clock.step", {"minutes": 1}).status_code == 200
    current = synchronize(source[3], reader, source[2].factory_id)
    document = export(publishing)
    assert document["candidate"] == original["candidate"]
    assert document["candidate"]["content_hash"] == digest
    assert document["original_snapshot_hash"] == original["original_snapshot_hash"]
    assert document["current_snapshot_hash"] == current.content_hash
    assert document["current_snapshot_hash"] != document["original_snapshot_hash"]
    assert document["current_checker"] is None and document["current_error_code"] is not None
    assert publications(source[3], source[2].factory_id) == []


@pytest.mark.parametrize("dynamic_source", [{"progress_revalidation": True}], indirect=True)
def test_export_clears_current_check_when_source_expires_during_progress_validation(
    progress, monkeypatch
):
    ctx = progress
    real_check = exports.check_progress
    checked = []

    class ExportClock:
        value = datetime.now(UTC)

        @classmethod
        def now(cls, timezone):
            return cls.value.astimezone(timezone)

    def delayed_validation(*args):
        proof = real_check(*args)
        checked.append(proof.checked.report)
        ExportClock.value += timedelta(seconds=31)
        return proof

    def decisions():
        with Session(ctx.engine) as db:
            return {
                "approvals": {
                    row.approval_id: row.document
                    for row in db.scalars(
                        select(ApprovalRecord).where(ApprovalRecord.factory_id == ctx.factory)
                    )
                },
                "publications": {
                    row.release_id: row.document
                    for row in db.scalars(
                        select(Publication).where(Publication.factory_id == ctx.factory)
                    )
                },
            }

    before = decisions()
    source_before = snapshot(ctx.source)
    monkeypatch.setattr(exports, "datetime", ExportClock)
    monkeypatch.setattr(exports, "check_progress", delayed_validation)
    document = export_candidate(
        ctx.engine,
        ctx.actor,
        ctx.factory,
        ctx.candidate.candidate_id,
        candidate_hash=ctx.candidate.content_hash,
    )
    assert len(checked) == 1 and checked[0].status == "PASS"
    assert document["original_checker"]["status"] == "PASS"
    assert document["candidate"] == ctx.candidate.model_dump(mode="json")
    assert document["current_snapshot_hash"] == ctx.current.content_hash
    assert document["current_checker"] is None
    assert document["current_error_code"] == "STALE_SOURCE"
    assert datetime.fromisoformat(document["exported_at"]) == ExportClock.value
    assert decisions() == before
    assert snapshot(ctx.source) == source_before


@pytest.mark.parametrize("dynamic_source", [{"read_only": True}], indirect=True)
def test_authenticated_export_get_creates_no_approval_or_publication(publishing):
    source, _, _, actor, candidate_id, digest = publishing
    engine, factory = source[3], source[2].factory_id
    password = uuid4().hex
    with Session(engine) as db, db.begin():
        db.get(User, actor.user_id).password_hash = hasher.hash(password)
    settings = Settings(
        _env_file=None,
        legacy_password_login_enabled=True,
        database_url=SecretStr(engine.url.render_as_string(hide_password=False)),
        llm_gateway_api_key=SecretStr(""),
    )
    with TestClient(create_app(settings)) as client:
        url = f"/api/factories/{factory}/candidates/{candidate_id}/export?candidate_hash={digest}"
        assert client.get(url).status_code == 401
        assert (
            client.post(
                "/api/login",
                json={"username": actor.username, "password": password},
                headers={"origin": "http://127.0.0.1:5173"},
            ).status_code
            == 200
        )
        first = client.get(url)
        assert first.status_code == 200
        assert first.headers["content-disposition"] == 'attachment; filename="byof-plan.json"'
        assert client.get(url).json()["candidate"] == first.json()["candidate"]
        assert first.json()["current_error_code"] is None
    with Session(engine) as db, db.begin():
        assert db.scalar(select(ApprovalRecord).where(ApprovalRecord.factory_id == factory)) is None
        assert db.scalar(select(Publication).where(Publication.factory_id == factory)) is None
        db.execute(delete(LoginSession).where(LoginSession.user_id == actor.user_id))
