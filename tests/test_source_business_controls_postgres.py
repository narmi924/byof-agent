"""New source facts use existing administrator HTTP authority, retries and recorded replay."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_cases_api_postgres import case_api as case_api
from test_cases_api_postgres import case_context as case_context
from test_cases_api_postgres import dynamic_source as dynamic_source
from test_cases_api_postgres import publishing as publishing
from test_dynamic_factory_postgres import control, snapshot
from test_factory_replay_postgres import finish, original_rows
from test_source_business_controls import overtime_payload, quote_payload, rule_payload

from packages.domain.execution import SimulatorCommand
from packages.domain.models import Event
from packages.domain.snapshot_delta import apply_delta
from packages.planning.service import synchronize
from services.factory_sim.replay import _physical_facts, start_replay
from services.factory_sim.service import command
from services.factory_sim.storage import SourceAction, SourceChange


def test_withdrawn_approved_overtime_is_suggested_read_only_then_handled_once(
    case_api, monkeypatch
):
    """The fixture runs only against the isolated byof_test source, never the preview."""
    from test_cases_api_postgres import ledger, normalized_case
    from test_source_business_controls import overtime_only_source

    from packages.agent import case_runtime
    from packages.agent.cases_store import CaseInput
    from packages.auth import Grant, Principal
    from packages.domain.models import Candidate
    from packages.persistence import Membership
    from packages.planning.publication import commit_publication, deliver_one
    from packages.planning.service import approve, request_solve
    from packages.planning.store import CandidateRecord, SolveJob
    from services.factory_sim.engine import evolve
    from services.factory_sim.service import _locked, _write
    from services.solver_worker.main import run_once

    ctx = case_api
    reader, writer, planner = ctx.context[1:4]
    before = snapshot(ctx.source)
    capacity = overtime_only_source(before)
    # Synthetic small capacity fixture, written before any approval. Real HTTP commands
    # below remove/add overtime, and the real approval + publication boundary is retained.
    capacity = evolve(before, resources=capacity.resources, workers=capacity.workers)
    with Session(ctx.source[4]) as db, db.begin():
        world = _locked(db, ctx.factory)
        _write(db, world, before, capacity, "test.capacity.fixture")
    synchronize(ctx.engine, reader, ctx.factory)
    with Session(ctx.engine) as db, db.begin():
        db.add(Membership(user_id=ctx.accounts["manager"], factory_id=ctx.factory, role="planner"))
    manager = Principal(
        user_id=ctx.accounts["manager"],
        username=ctx.accounts["manager"],
        grants=(
            Grant(factory_id=ctx.factory, role="manager"),
            Grant(factory_id=ctx.factory, role="planner"),
        ),
    )
    job = request_solve(
        ctx.engine,
        planner,
        ctx.factory,
        request_id="overtime-plan",
        allow_overtime=True,
        time_limit=3,
    )
    assert run_once(ctx.engine)
    with Session(ctx.engine) as db:
        job = db.get(SolveJob, job.job_id)
        assert job.state == "SUCCEEDED"
        plan = Candidate.model_validate(db.get(CandidateRecord, job.candidate_id).document)
    assert plan.has_solution and "allow_overtime" in plan.required_consents
    for scope, actor in (("allow_overtime", manager), ("publish_plan", planner)):
        approve(
            ctx.engine,
            actor,
            ctx.factory,
            plan.candidate_id,
            request_id="approve-" + scope,
            candidate_hash=plan.content_hash,
            action_scope=scope,
            decision="APPROVED",
        )
    commit_publication(
        ctx.engine,
        planner,
        ctx.factory,
        plan.candidate_id,
        request_id="publish-overtime",
        candidate_hash=plan.content_hash,
    )
    assert deliver_one(ctx.engine, reader, writer)
    active = synchronize(ctx.engine, reader, ctx.factory)
    assert active.active_plan_hash == plan.content_hash
    row = next(item for item in active.workers if item.worker_id == plan.assignments[0].worker_id)
    window = next(item for item in row.calendar if item.kind == "OVERTIME")
    command_body = {
        "request_id": "withdraw-approved-overtime",
        "run_id": active.run_id,
        "kind": "overtime_window.set",
        "payload": {
            "target_type": "worker",
            "target_id": row.worker_id,
            "expected_version": row.version,
            "action": "remove",
            "start_at": window.start_at.isoformat(),
            "end_at": window.end_at.isoformat(),
        },
    }
    admin, admin_headers = ctx.login("sim_admin")
    commands = f"/api/admin/factories/{ctx.factory}/simulator/commands"
    response = admin.post(commands, json=command_body, headers=admin_headers)
    assert response.status_code == 200, response.text
    assert admin.post(commands, json=command_body, headers=admin_headers).json() == response.json()
    current = synchronize(ctx.engine, reader, ctx.factory)
    assert current.active_plan_hash == active.active_plan_hash and current.actuals == active.actuals
    with Session(ctx.source[4]) as db:
        recorded = db.get(SourceChange, (current.run_id, int(current.source.source_revision)))
        event_id = next(
            item["event_id"]
            for item in recorded.document["events"]
            if item["entity_type"] == "workers"
        )

    def no_model(*args, **kwargs):
        pytest.fail("Reading or acknowledging source risk must not run an Agent/model")

    monkeypatch.setattr(case_runtime, "process_case", no_model)
    monkeypatch.setattr(case_runtime, "_decide", no_model)
    manager_client, manager_headers = ctx.login("manager")
    read_url = f"{ctx.base}/risk-suggestions"
    before_read = ledger(ctx)
    first = manager_client.get(read_url)
    second = manager_client.get(read_url)
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert ledger(ctx) == before_read and snapshot(ctx.source) == current
    assert len(first.json()["suggestions"]) == 1
    suggestion = first.json()["suggestions"][0]
    assert suggestion["title"] == "Check the revoked overtime window"
    assert "unfinished operation" in suggestion["detail"]
    click = {
        "request_id": "manager-ack-overtime-risk",
        "message": suggestion["prompt"],
        "suggestion_id": suggestion["suggestion_id"],
        "start_new": True,
    }
    created = manager_client.post(f"{ctx.base}/cases", json=click, headers=manager_headers)
    assert created.status_code == 200, created.text
    retry = manager_client.post(f"{ctx.base}/cases", json=click, headers=manager_headers)
    assert retry.status_code == 200
    assert normalized_case(retry.json()) == normalized_case(created.json())
    assert manager_client.get(read_url).json()["suggestions"] == []
    with Session(ctx.engine) as db:
        inputs = list(
            db.scalars(select(CaseInput).where(CaseInput.case_id == created.json()["case_id"]))
        )
        assert len(inputs) == 1 and inputs[0].payload["source_event_ids"] == [event_id]


def test_new_source_fact_controls_preserve_administrator_scope_and_one_effect_on_retry(case_api):
    ctx = case_api
    source = ctx.source
    current = snapshot(source)
    original = current
    url = f"/api/admin/factories/{ctx.factory}/simulator/commands"
    for index, kind in enumerate(
        ("delivery_rule.set", "expedite_quote.set", "expedite_quote.remove", "overtime_window.set")
    ):
        payload = (
            rule_payload(current)
            if kind == "delivery_rule.set"
            else quote_payload(current)
            if kind == "expedite_quote.set"
            else {
                "expected_terms_version": current.business_terms.version,
                "quote_id": "SUPPLIER-QUOTE",
            }
            if kind == "expedite_quote.remove"
            else overtime_payload(current)
        )
        body = {
            "request_id": f"new-condition-{index}",
            "run_id": current.run_id,
            "kind": kind,
            "payload": payload,
        }
        for role in ("planner", "manager", "maintainer", "outsider"):
            client, headers = ctx.login(role)
            assert client.post(url, json=body, headers=headers).status_code == 403
            assert snapshot(source) == current
        client, headers = ctx.login("sim_admin")
        denied = client.post(
            url.replace(ctx.factory, ctx.factory + "-other"), json=body, headers=headers
        )
        assert denied.status_code == 403
        response = client.post(url, json=body, headers=headers)
        assert response.status_code == 200, response.text
        updated = snapshot(source)
        retry = client.post(url, json=body, headers=headers)
        assert retry.status_code == 200 and retry.json() == response.json()
        assert snapshot(source) == updated
        assert updated.orders == original.orders and updated.receipts == original.receipts
        assert updated.inventory == original.inventory and updated.actuals == original.actuals
        assert updated.active_plan_hash == original.active_plan_hash
        with Session(source[4]) as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(SourceAction)
                    .where(
                        SourceAction.factory_id == ctx.factory,
                        SourceAction.operation_id == body["request_id"],
                    )
                )
                == 1
            )
            change = db.get(SourceChange, (updated.run_id, int(updated.source.source_revision)))
            assert apply_delta(current, change.document["snapshot_delta"]) == updated
            assert any(
                Event.model_validate(item).event_type == kind for item in change.document["events"]
            )
        synced = synchronize(ctx.engine, ctx.context[1], ctx.factory)
        assert synced == updated
        current = updated


def test_stale_condition_forms_are_rejected_without_partial_effect_and_quote_stays_stale(
    dynamic_source,
):
    source = dynamic_source
    initial = snapshot(source)
    payload = rule_payload(initial)
    assert control(source, "rule", "delivery_rule.set", payload).status_code == 200
    configured = snapshot(source)
    assert (
        control(
            source,
            "stale-rule",
            "delivery_rule.set",
            {**payload, "partial_delivery_allowed": False},
        ).json()["code"]
        == "BUSINESS_TERMS_CHANGED"
    )
    assert snapshot(source) == configured
    quote = quote_payload(configured)
    assert control(source, "quote", "expedite_quote.set", quote).status_code == 200
    quoted = snapshot(source)
    receipt = next(r for r in quoted.receipts if r.receipt_id == quote["receipt_id"])
    assert (
        control(
            source,
            "delay",
            "receipt.delay",
            {
                "receipt_id": receipt.receipt_id,
                "eta": (receipt.eta + timedelta(hours=1)).isoformat(),
            },
        ).status_code
        == 200
    )
    delayed = snapshot(source)
    assert delayed.business_terms == quoted.business_terms
    assert (
        control(
            source,
            "stale-quote",
            "expedite_quote.set",
            {**quote, "expected_terms_version": delayed.business_terms.version},
        ).json()["code"]
        == "RECEIPT_VERSION_CHANGED"
    )
    assert snapshot(source) == delayed
    window = overtime_payload(delayed)
    assert control(source, "overtime", "overtime_window.set", window).status_code == 200
    changed = snapshot(source)
    assert (
        control(
            source, "stale-window", "overtime_window.set", {**window, "action": "remove"}
        ).json()["code"]
        == "RESOURCE_VERSION_CHANGED"
    )
    assert snapshot(source) == changed


def test_new_business_fact_commands_replay_the_same_terms_calendars_and_preserve_original_rows(
    dynamic_source,
):
    source = dynamic_source
    for request_id, kind in (
        ("rule", "delivery_rule.set"),
        ("quote", "expedite_quote.set"),
        ("worker-window", "overtime_window.set"),
        ("resource-window", "overtime_window.set"),
    ):
        current = snapshot(source)
        payload = (
            rule_payload(current)
            if kind == "delivery_rule.set"
            else quote_payload(current)
            if kind == "expedite_quote.set"
            else overtime_payload(
                current, "resource" if request_id.startswith("resource") else "worker"
            )
        )
        assert control(source, request_id, kind, payload).status_code == 200
    current = snapshot(source)
    assert (
        control(
            source,
            "remove-quote",
            "expedite_quote.remove",
            {
                "expected_terms_version": current.business_terms.version,
                "quote_id": "SUPPLIER-QUOTE",
            },
        ).status_code
        == 200
    )
    final = snapshot(source)
    history = original_rows(source[4], final.run_id)
    replay = start_replay(source[4], final.factory_id, "replay-new-conditions", final.run_id)
    result, state = finish(source[4], final.factory_id)
    assert state["physical_match"]
    assert _physical_facts(result) == _physical_facts(final)
    assert original_rows(source[4], final.run_id) == history
    # Replay remains read-only through the existing command boundary.
    import pytest

    from services.factory_sim.engine import SimulationError

    with pytest.raises(SimulationError) as forbidden:
        command(
            source[4],
            final.factory_id,
            SimulatorCommand(
                request_id="no-replay-write",
                run_id=replay["run_id"],
                kind="delivery_rule.set",
                payload=rule_payload(result),
            ),
        )
    assert forbidden.value.code == "REPLAY_READ_ONLY"
