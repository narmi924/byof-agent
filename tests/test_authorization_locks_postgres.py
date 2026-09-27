"""Real PostgreSQL authorization readers coexist while revocation stays transactional."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from queue import Queue
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session
from test_checker import example_snapshot, make_candidate

from packages.auth import AccessError, Grant, Principal, lock_membership, lock_user
from packages.domain.models import Candidate, ConnectorCapabilities, Snapshot
from packages.integrations import contacts
from packages.integrations.notification_store import ContactAction, NotificationContact
from packages.persistence import Membership, User, connect
from packages.planning import publication
from packages.planning.checker import check_candidate
from packages.planning.service import approve, workspace
from packages.planning.store import ApprovalRecord, CandidateRecord, FactoryState, SnapshotRecord


@dataclass(frozen=True)
class Plan:
    factory_id: str
    snapshot: Snapshot
    candidate: Candidate
    requester: Principal
    approver: Principal


@pytest.fixture
def plans():
    app_url = os.environ.get("TEST_DATABASE_URL")
    owner_url = os.environ.get("TEST_MIGRATION_DATABASE_URL")
    if not app_url or not owner_url:
        pytest.skip("TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL required")
    engine, owner = connect(app_url), connect(owner_url)
    assert engine.url.database == owner.url.database == "byof_test"
    users = tuple("lock-user-" + uuid4().hex for _ in range(2))
    factories = tuple("lock-factory-" + uuid4().hex for _ in range(2))
    actors = tuple(
        Principal(
            user_id=identity,
            username=identity,
            grants=tuple(
                Grant(factory_id=factory, role="planner" if i == j else "manager")
                for j, factory in enumerate(factories)
            ),
        )
        for i, identity in enumerate(users)
    )
    items = []
    try:
        with Session(engine) as db, db.begin():
            for actor in actors:
                db.add(
                    User(
                        user_id=actor.user_id,
                        username=actor.username,
                        password_hash="not-a-login",
                        active=True,
                    )
                )
            db.flush()
            for actor in actors:
                for grant in actor.grants:
                    db.add(
                        Membership(
                            user_id=actor.user_id, factory_id=grant.factory_id, role=grant.role
                        )
                    )
            for index, factory in enumerate(factories):
                data = example_snapshot().model_dump(mode="json", exclude={"content_hash"})
                data.update(factory_id=factory, snapshot_id=str(uuid4()), run_id=str(uuid4()))
                data["profile"]["factory_id"] = factory
                snapshot = Snapshot.model_validate(data)
                candidate = make_candidate(snapshot, candidate_id=str(uuid4()), allow_overtime=True)
                report = check_candidate(snapshot, candidate, allow_overtime=True)
                assert report.status == "PASS"
                document = candidate.model_dump(mode="json", exclude={"content_hash"})
                document["checker"] = report.model_dump(mode="json")
                candidate = Candidate.model_validate(document)
                now = datetime.now(UTC)
                db.add(
                    SnapshotRecord(
                        snapshot_id=snapshot.snapshot_id,
                        factory_id=factory,
                        content_hash=snapshot.content_hash,
                        document=snapshot.model_dump(mode="json"),
                        created_at=now,
                    )
                )
                db.flush()
                db.add(
                    FactoryState(
                        factory_id=factory,
                        snapshot_id=snapshot.snapshot_id,
                        run_id=snapshot.run_id,
                        source_revision=snapshot.source.source_revision,
                        last_synced_at=now,
                        connector_capabilities=ConnectorCapabilities(
                            read_snapshot=True,
                            read_changes=True,
                            query_detail=True,
                            accept_plan=True,
                            query_action=True,
                            idempotency=True,
                            conditional_acceptance=True,
                            snapshot_consistency="ATOMIC_SNAPSHOT",
                        ).model_dump(mode="json"),
                        capabilities_observed_at=now,
                    )
                )
                db.add(
                    CandidateRecord(
                        candidate_id=candidate.candidate_id,
                        factory_id=factory,
                        snapshot_id=snapshot.snapshot_id,
                        content_hash=candidate.content_hash,
                        document=candidate.model_dump(mode="json"),
                        created_at=now,
                    )
                )
                items.append(Plan(factory, snapshot, candidate, actors[index], actors[1 - index]))
        for item in items:
            for actor, scope in (
                (item.requester, "publish_plan"),
                (item.approver, "allow_overtime"),
            ):
                approve(
                    engine,
                    actor,
                    item.factory_id,
                    item.candidate.candidate_id,
                    request_id=str(uuid4()),
                    candidate_hash=item.candidate.content_hash,
                    action_scope=scope,
                    decision="APPROVED",
                )
        yield engine, owner, tuple(items)
    finally:
        with owner.begin() as db:
            for record in (
                ContactAction,
                NotificationContact,
                publication.Publication,
                ApprovalRecord,
                CandidateRecord,
                FactoryState,
                SnapshotRecord,
            ):
                db.execute(delete(record).where(record.factory_id.in_(factories)))
            db.execute(delete(Membership).where(Membership.user_id.in_(users)))
            db.execute(delete(User).where(User.user_id.in_(users)))
        engine.dispose()
        owner.dispose()


def _commit(engine, plan, request_id="publish"):
    return publication.commit_publication(
        engine,
        plan.requester,
        plan.factory_id,
        plan.candidate.candidate_id,
        request_id=request_id,
        candidate_hash=plan.candidate.content_hash,
    )


def _publications(engine, plans):
    with Session(engine) as db:
        return db.scalars(
            select(publication.Publication).where(
                publication.Publication.factory_id.in_(p.factory_id for p in plans)
            )
        ).all()


def _revoke(db, plan, subject, kind):
    actor = plan.requester if subject == "requester" else plan.approver
    role = "planner" if subject == "requester" else "manager"
    if kind == "account":
        db.execute(update(User).where(User.user_id == actor.user_id).values(active=False))
    else:
        db.execute(
            delete(Membership).where(
                Membership.user_id == actor.user_id,
                Membership.factory_id == plan.factory_id,
                Membership.role == role,
            )
        )


def _wait_for_blocker(engine, pid):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with engine.connect() as db:
            if db.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": pid}):
                return
        sleep(0.01)
    pytest.fail("Revocation did not wait for the authorization transaction")


def test_reverse_requester_and_approver_order_commits_both_factories(plans, monkeypatch):
    engine, _, items = plans
    barrier = Barrier(2, timeout=5)
    original = publication._authorized

    def controlled(db, factory_id, user_id, role):
        db.execute(text("SET LOCAL statement_timeout = '6s'"))
        valid = original(db, factory_id, user_id, role)
        if not db.info.get("first_authorization"):
            db.info["first_authorization"] = True
            barrier.wait()
        return valid

    monkeypatch.setattr(publication, "_authorized", controlled)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_commit, engine, plan) for plan in items]
        releases = [future.result(timeout=10) for future in futures]
    assert len({release.release_id for release in releases}) == 2
    rows = _publications(engine, items)
    assert len(rows) == 2
    for row in rows:
        assert row.state == "QUEUED" and row.attempts == 0
        assert row.document["local_state"] == "LOCAL_COMMITTED"
        assert row.document["source_state"] == "PENDING_SOURCE"
        assert len(row.document["approval_ids"]) == 2
        assert row.payload["candidate"]["factory_id"] == row.factory_id


def test_reverse_administrator_and_contact_owner_order_updates_both_factories(plans, monkeypatch):
    engine, _, items = plans
    barrier = Barrier(2, timeout=5)
    original = contacts.live_actor
    admins = []
    with Session(engine) as db, db.begin():
        for plan in items:
            db.add(
                Membership(user_id=plan.requester.user_id, factory_id=plan.factory_id, role="admin")
            )
            admins.append(
                Principal(
                    user_id=plan.requester.user_id,
                    username=plan.requester.username,
                    grants=(
                        *plan.requester.grants,
                        Grant(factory_id=plan.factory_id, role="admin"),
                    ),
                )
            )

    def controlled(db, actor, factory_id, roles, *, lock=False):
        db.execute(text("SET LOCAL statement_timeout = '6s'"))
        original(db, actor, factory_id, roles, lock=lock)
        barrier.wait()

    monkeypatch.setattr(contacts, "live_actor", controlled)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                contacts.configure_contact,
                engine,
                admin,
                plan.factory_id,
                contacts.ContactInput(
                    request_id=str(uuid4()),
                    role="manager",
                    user_id=plan.approver.user_id,
                    email="authorized-contact@example.invalid",
                    enabled=True,
                    expected_version=0,
                ),
            )
            for admin, plan in zip(admins, items)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert {result["user_id"] for result in results} == {p.approver.user_id for p in items}
    assert all(result["version"] == 1 and result["enabled"] for result in results)
    with Session(engine) as db:
        records = db.scalars(
            select(NotificationContact).where(
                NotificationContact.factory_id.in_(p.factory_id for p in items)
            )
        ).all()
        assert len(records) == 2
        assert {(row.factory_id, row.user_id) for row in records} == {
            (p.factory_id, p.approver.user_id) for p in items
        }
    assert _publications(engine, items) == []


@pytest.mark.parametrize("subject", ["requester", "approver"])
@pytest.mark.parametrize("kind", ["account", "membership"])
def test_committed_revocation_prevents_publication_without_side_effect(plans, subject, kind):
    engine, owner, items = plans
    plan = items[0]
    with owner.begin() as db:
        _revoke(db, plan, subject, kind)
    with pytest.raises(AccessError) as denied:
        _commit(engine, plan)
    assert denied.value.code == ("FORBIDDEN" if subject == "requester" else "APPROVAL_REQUIRED")
    assert _publications(engine, items) == []


@pytest.mark.parametrize("kind", ["account", "membership"])
def test_authorization_refreshes_previously_cached_identity(plans, kind):
    engine, owner, items = plans
    plan = items[0]
    with Session(engine) as db, db.begin():
        cached_user = db.get(User, plan.requester.user_id)
        cached_role = db.get(Membership, (plan.requester.user_id, plan.factory_id, "planner"))
        assert cached_user.active and cached_role is not None
        with owner.begin() as other:
            _revoke(other, plan, "requester", kind)
        assert not publication._authorized(db, plan.factory_id, plan.requester.user_id, "planner")
        if kind == "account":
            assert not cached_user.active
        else:
            assert lock_membership(db, plan.requester.user_id, plan.factory_id, "planner") is None
    assert _publications(engine, items) == []


@pytest.mark.parametrize("kind", ["account", "membership"])
def test_revocation_waits_for_authorized_publication_commit(plans, monkeypatch, kind):
    engine, owner, items = plans
    plan = items[0]
    pinned, allow_commit = Event(), Event()
    pid = Queue()
    original = publication._authorized

    def controlled(db, factory_id, user_id, role):
        db.execute(text("SET LOCAL statement_timeout = '8s'"))
        valid = original(db, factory_id, user_id, role)
        if not db.info.get("first_authorization"):
            db.info["first_authorization"] = True
            pinned.set()
            assert allow_commit.wait(8), "Test did not release the authorization transaction"
        return valid

    def revoke():
        with owner.begin() as db:
            db.execute(text("SET LOCAL statement_timeout = '8s'"))
            pid.put(db.scalar(text("SELECT pg_backend_pid()")))
            _revoke(db, plan, "requester", kind)

    monkeypatch.setattr(publication, "_authorized", controlled)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(_commit, engine, plan)
        try:
            assert pinned.wait(5)
            revocation = pool.submit(revoke)
            _wait_for_blocker(engine, pid.get(timeout=5))
            assert not revocation.done()
        finally:
            allow_commit.set()
        release = publishing.result(timeout=10)
        revocation.result(timeout=10)
    assert release.source_state == "PENDING_SOURCE"
    assert len(_publications(engine, items)) == 1
    monkeypatch.setattr(publication, "_authorized", original)
    with pytest.raises(AccessError) as denied:
        _commit(engine, plan, request_id="after-revocation")
    assert denied.value.code == "FORBIDDEN"
    assert len(_publications(engine, items)) == 1
    with Session(engine) as db, db.begin():
        user = lock_user(db, plan.requester.user_id)
        if kind == "account":
            assert user is not None and not user.active
        else:
            assert user is not None and user.active
            assert lock_membership(db, user.user_id, plan.factory_id, "planner") is None


@pytest.mark.parametrize("kind", ["user", "role"])
def test_workspace_retains_decisions_but_does_not_show_revoked_approval_as_valid(plans, kind):
    engine, owner, items = plans
    plan = items[0]
    original = workspace(engine, plan.factory_id)["candidates"][0]
    assert original["state"] == "APPROVED"
    with owner.begin() as db:
        _revoke(db, plan, "approver", kind)
    current = workspace(engine, plan.factory_id)["candidates"][0]
    assert current["state"] == "CANDIDATE"
    assert current["approvals"] == original["approvals"]
    assert current["candidate"] == original["candidate"]
