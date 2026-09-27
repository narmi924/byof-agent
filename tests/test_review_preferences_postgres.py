"""Preference confirmation cancels obsolete review work in the same transaction."""

import os

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_handoff_postgres import accept
from test_handoff_postgres import task as handoff_task
from test_human_review_postgres import origin_task, pending_notifications
from test_human_review_postgres import review_context as review_context
from test_human_tasks_postgres import human_context as human_context
from test_preferences_postgres import confirm, current, definition, propose

from packages.agent.human_tasks import get_task
from packages.integrations.notification_store import Notification
from packages.persistence import connect
from packages.planning.preference_store import (
    ObjectiveRecord,
    PreferenceAction,
    PreferenceCoordination,
    PreferenceHead,
    PreferenceProposal,
    PreferenceRevision,
    PreferenceState,
)
from packages.planning.store import SnapshotRecord


@pytest.fixture
def context(review_context):
    ctx = review_context
    try:
        yield ctx
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        with owner.begin() as db:
            for model in (
                ObjectiveRecord,
                PreferenceAction,
                PreferenceCoordination,
                PreferenceHead,
                PreferenceRevision,
                PreferenceProposal,
                PreferenceState,
            ):
                db.execute(delete(model).where(model.factory_id == ctx.factory))
        owner.dispose()


def test_confirmed_preference_cancels_review_and_unsent_reminders_without_sync(context):
    ctx = context
    task = origin_task(ctx)
    pending_notifications(ctx, task)
    proposal = propose(ctx, scope="CASE", selected=definition("stability_first"))
    assert (
        get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"])["state"] == "OPEN"
    )
    confirmation = confirm(ctx, proposal, request_id="confirmed-change")
    after = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"])
    assert after["state"] == "CANCELLED"
    assert after["response"]["outcome"] == "STALE"
    assert after["response"]["reason"] == "OBJECTIVE_CHANGED"
    assert confirm(ctx, proposal, request_id="confirmed-change") == confirmation
    assert get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"]) == after
    with Session(ctx.engine) as db:
        notices = list(
            db.scalars(select(Notification).where(Notification.task_id == task["task_id"]))
        )
        assert sorted(row.send_state for row in notices) == ["CANCELLED", "CANCELLED", "UNKNOWN"]
        assert (
            db.get(SnapshotRecord, ctx.snapshot.snapshot_id).content_hash
            == ctx.snapshot.content_hash
        )


def test_explicit_handoff_removes_case_preference_from_shared_objective(context):
    ctx = context
    confirm(ctx, propose(ctx, scope="CASE", selected=definition("stability_first")))
    before = current(ctx)
    assert before.definition.selection == "stability_first"
    assert [source.scope_id for source in before.sources] == [ctx.case_id]
    request = handoff_task(ctx)
    accepted = accept(ctx, request)
    assert accepted["state"] == "ACCEPTED"
    after = current(ctx)
    assert after.definition.selection == "delivery_first"
    assert after.sources == ()
    assert after.objective_version != before.objective_version
