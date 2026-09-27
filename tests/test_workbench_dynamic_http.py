from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages.auth import digest
from packages.integrations.sync import RunSwitch
from packages.persistence import LoginSession, Membership
from packages.planning.store import FactoryState
from packages.settings import Settings
from services.api.main import create_app
from services.factory_sim.storage import SourceAction


def test_authenticated_controls_require_csrf_and_sim_admin_and_get_never_executes(publishing):
    source, _, _, actor, candidate_id, candidate_hash = publishing
    transport, tokens, initial, engine, sim_engine = source
    session_token, csrf = uuid4().hex, uuid4().hex
    with Session(engine) as db, db.begin():
        db.add(
            LoginSession(
                token_hash=digest(session_token),
                csrf_hash=digest(csrf),
                user_id=actor.user_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    app = create_app(
        Settings(
            _env_file=None,
            database_url=engine.url.render_as_string(hide_password=False),
            factory_api_url=str(transport.base_url),
            factory_api_token=tokens["reader"],
            factory_control_token=tokens["controller"],
            public_origin="http://testserver",
        )
    )
    try:
        with TestClient(app) as client:
            client.cookies.set("byof_session", session_token)
            prefix = f"/api/admin/factories/{initial.factory_id}/simulator"
            body = {
                "request_id": "authorized-step",
                "run_id": initial.run_id,
                "kind": "clock.step",
                "payload": {"minutes": 1},
            }
            headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf}
            assert client.get(prefix).status_code == 403
            assert client.post(prefix + "/commands", json=body, headers=headers).status_code == 403
            with Session(engine) as db, db.begin():
                db.add(
                    Membership(
                        user_id=actor.user_id, factory_id=initial.factory_id, role="sim_admin"
                    )
                )
            assert client.get(prefix).json()["mode"] == "PAUSED"
            assert client.get(prefix + "/commands").status_code == 405
            assert client.post(prefix + "/commands", json=body).status_code == 403
            assert (
                client.post(
                    prefix + "/commands",
                    json=body,
                    headers={**headers, "Origin": "http://hostile.invalid"},
                ).status_code
                == 403
            )
            response = client.post(prefix + "/commands", json=body, headers=headers)
            assert response.status_code == 200
            assert (
                client.post(prefix + "/commands", json=body, headers=headers).json()
                == response.json()
            )
            current = client.get(f"/api/factories/{initial.factory_id}/workspace").json()
            assert (
                current["snapshot"]["snapshot_clock"]
                != initial.model_dump(mode="json")["snapshot_clock"]
            )
            assert current["snapshot"]["actuals"] == []
            with Session(sim_engine) as db:
                actions = db.scalars(
                    select(SourceAction).where(SourceAction.factory_id == initial.factory_id)
                ).all()
                assert len(actions) == 1 and actions[0].kind == "clock.step"
            assert (
                client.post(
                    prefix.replace(initial.factory_id, "other") + "/commands",
                    json=body,
                    headers=headers,
                ).status_code
                == 403
            )
            publish = f"/api/factories/{initial.factory_id}/candidates/{candidate_id}/publications"
            assert client.get(publish).status_code == 405
            assert (
                client.post(
                    publish,
                    json={
                        "request_id": "forged",
                        "candidate_hash": candidate_hash,
                        "confirmed": True,
                    },
                    headers=headers,
                ).status_code
                == 422
            )
            assert current["freshness"] == "CURRENT"
            with Session(engine) as db, db.begin():
                db.execute(
                    update(FactoryState)
                    .where(FactoryState.factory_id == initial.factory_id)
                    .values(last_synced_at=datetime.now(UTC) - timedelta(seconds=31))
                )
            assert (
                client.get(f"/api/factories/{initial.factory_id}/workspace").json()["freshness"]
                == "STALE"
            )
            replay_body = {"request_id": "replay-run", "expected_run_id": initial.run_id}
            assert client.get(prefix + "/replays").status_code == 405
            assert client.post(prefix + "/replays", json=replay_body).status_code == 403
            replay = client.post(prefix + "/replays", json=replay_body, headers=headers)
            assert replay.status_code == 200
            replay_id = replay.json()["run_id"]
            assert replay_id != initial.run_id
            assert (
                client.post(prefix + "/replays", json=replay_body, headers=headers).json()
                == replay.json()
            )
            with Session(engine) as db:
                switches = db.scalars(
                    select(RunSwitch).where(RunSwitch.factory_id == initial.factory_id)
                ).all()
                assert len(switches) == 1
                assert switches[0].previous_run_id == initial.run_id
                assert switches[0].run_id == replay_id
                assert switches[0].actor_id == actor.user_id
            state = client.get(f"/api/factories/{initial.factory_id}/workspace").json()
            assert state["snapshot"]["run_id"] == replay_id and state["freshness"] == "CURRENT"
            assert state["snapshot"]["source"]["source_system"] == "factory-simulator-replay"
            injected = {
                **body,
                "run_id": replay_id,
                "request_id": "denied",
                "kind": "resource.down",
                "payload": {"resource_id": "M1"},
            }
            assert (
                client.post(prefix + "/commands", json=injected, headers=headers).json()["code"]
                == "REPLAY_READ_ONLY"
            )
            assert (
                client.post(
                    f"/api/factories/{initial.factory_id}/solve",
                    json={"request_id": "no-replay-solve"},
                    headers=headers,
                ).json()["code"]
                == "REPLAY_READ_ONLY"
            )
            assert (
                client.post(
                    publish,
                    json={"request_id": "no-replay-publish", "candidate_hash": candidate_hash},
                    headers=headers,
                ).json()["code"]
                == "REPLAY_READ_ONLY"
            )
            approvals = f"/api/factories/{initial.factory_id}/candidates/{candidate_id}/approvals"
            assert (
                client.post(
                    approvals,
                    json={
                        "request_id": "no-replay-approval",
                        "candidate_hash": candidate_hash,
                        "action_scope": "publish_plan",
                        "decision": "APPROVED",
                    },
                    headers=headers,
                ).json()["code"]
                == "REPLAY_READ_ONLY"
            )
            stepped = {**body, "run_id": replay_id, "request_id": "replay-step"}
            assert (
                client.post(prefix + "/commands", json=stepped, headers=headers).status_code == 200
            )
            replay_status = client.get(prefix).json()["replay"]
            assert replay_status["done"] and replay_status["error_code"] is None
            assert set(replay_status) == {
                "origin_run_id",
                "next_revision",
                "target_revision",
                "done",
                "error_code",
            }
            run = {**stepped, "request_id": "no-restart", "kind": "clock.run", "payload": {}}
            assert (
                client.post(prefix + "/commands", json=run, headers=headers).json()["code"]
                == "REPLAY_FINISHED"
            )
            with Session(engine) as db, db.begin():
                db.execute(
                    delete(Membership).where(
                        Membership.user_id == actor.user_id, Membership.role == "sim_admin"
                    )
                )
            assert (
                client.post(prefix + "/replays", json=replay_body, headers=headers).status_code
                == 403
            )
    finally:
        with engine.begin() as db:
            db.execute(delete(LoginSession).where(LoginSession.user_id == actor.user_id))
