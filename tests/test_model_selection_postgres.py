"""User isolation, optimistic updates and pinned turns on real PostgreSQL."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_case_runtime_postgres import case_context as case_context
from test_case_runtime_postgres import wait_action
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages.agent.case_runtime import process_case
from packages.agent.cases import create_case, message_case
from packages.agent.cases_store import CaseTurn
from packages.auth import AccessError
from packages.providers.catalog import ModelCatalog
from packages.providers.selection import UserModelRouter, UserModelSelection, choose, view
from packages.settings import Settings


def test_user_selection_is_persistent_versioned_and_does_not_change_other_users(case_context):
    context, _ = case_context
    source, _, _, actor, *_ = context
    engine = source[3]
    catalog = ModelCatalog(
        Settings(
            _env_file=None,
            llm_provider="deepseek",
            gateway_url="https://gateway.example",
            gateway_api_key="controlled",
            deepseek_api_key="controlled",
        )
    )
    try:
        result = choose(
            engine, actor, catalog, model_id="claude", expected_version=0, request_id="first"
        )
        assert result["selected_model_id"] == "claude" and result["version"] == 1
        assert (
            choose(
                engine, actor, catalog, model_id="claude", expected_version=0, request_id="first"
            )
            == result
        )
        with pytest.raises(AccessError) as conflict:
            choose(
                engine, actor, catalog, model_id="deepseek", expected_version=0, request_id="stale"
            )
        assert conflict.value.code == "MODEL_SELECTION_CHANGED"
        with Session(engine) as db:
            assert view(db, actor.user_id, catalog)["selected_model_id"] == "claude"
            assert view(db, "different-user", catalog)["selected_model_id"] == "deepseek"
        with pytest.raises(AccessError):
            choose(
                engine,
                actor.model_copy(update={"grants": ()}),
                catalog,
                model_id="claude",
                expected_version=1,
                request_id="forbidden",
            )
    finally:
        with Session(engine) as db, db.begin():
            db.execute(
                delete(UserModelSelection).where(UserModelSelection.user_id == actor.user_id)
            )


def test_running_turn_keeps_model_and_next_turn_uses_new_preference(case_context, monkeypatch):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine = source[3]
    catalog = ModelCatalog(
        Settings(
            _env_file=None,
            llm_provider="deepseek",
            gateway_url="https://gateway.example",
            gateway_api_key="controlled",
            deepseek_api_key="controlled",
        )
    )
    calls = []

    class Controlled:
        def __init__(self, name):
            self.name = name

        def complete(self, prompt):
            calls.append(self.name)
            if self.name == "deepseek" and len(calls) == 1:
                choose(
                    engine,
                    actor,
                    catalog,
                    model_id="claude",
                    expected_version=0,
                    request_id="during-call",
                )
            return wait_action()

    monkeypatch.setattr(catalog, "model", lambda name: Controlled(name))
    router = UserModelRouter(catalog, Controlled("unused-default"))
    try:
        assert process_case(engine, reader, router)
        message_case(
            engine, actor, source[2].factory_id, case["case_id"], uuid4().hex, "Keep verifying"
        )
        assert process_case(engine, reader, router)
        assert calls == ["deepseek", "claude"]
        other = create_case(
            engine,
            actor,
            source[2].factory_id,
            uuid4().hex,
            "A new production question",
            start_new=True,
        )
        assert other["case_id"] != case["case_id"]
        assert process_case(engine, reader, router)
        assert calls == ["deepseek", "claude", "claude"]
        with Session(engine) as db:
            assert set(
                db.scalars(select(CaseTurn.model_id).where(CaseTurn.case_id == case["case_id"]))
            ) == {"deepseek", "claude"}
            assert router.for_user(db, actor.user_id, "deepseek")[0] == "deepseek"
    finally:
        with Session(engine) as db, db.begin():
            db.execute(
                delete(UserModelSelection).where(UserModelSelection.user_id == actor.user_id)
            )
