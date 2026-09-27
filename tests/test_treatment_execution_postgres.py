"""Manager approval to source effects and publication on isolated PostgreSQL."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from test_assistant_postgres import request
from test_assistant_postgres import reviewer as reviewer
from test_case_runtime_postgres import case_context as case_context
from test_dynamic_factory_postgres import control, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages.agent.assistant_store import AssistantAction
from packages.agent.case_runtime import _execution_reply
from packages.agent.cases_store import CaseInput, CaseRecord
from packages.agent.treatment_execution import (
    ExecutionDecision,
    decide_execution,
    process_treatment,
)
from packages.auth import AccessError
from packages.domain.business_options import BusinessStudyRequest
from packages.domain.execution import SimulatorCommand
from packages.domain.models import canonical_hash
from packages.integrations.factory_http import ConnectorError
from packages.persistence import Membership
from packages.planning.business_service import request_business_study, study_view
from packages.planning.publication import deliver_one
from packages.planning.service import synchronize
from packages.planning.store import SolveJob
from services.factory_sim.storage import SourceAction
from services.solver_worker.main import run_once


@pytest.mark.parametrize(
    "scenario",
    [
        "normal",
        "immediate",
        "standard",
        "staff",
        "repair",
        "order_quantity",
        "order_due",
        "clock_only",
        "clock_race",
        "changed_facts",
    ],
)
@pytest.mark.parametrize("cancel", [False, True])
def test_one_manager_approval_executes_and_publishes_without_simulator_user(
    reviewer, scenario, cancel
):
    ctx = reviewer
    source, reader, writer, actor, _, _, controls, case = ctx
    engine, factory = source[3], source[2].factory_id
    if scenario == "order_quantity":
        order = source[2].orders[0]
        assert (
            control(
                source,
                "customer-increase",
                "order.revise",
                {
                    "order_id": order.order_id,
                    "expected_version": order.version,
                    "quantity": order.quantity * 2,
                    "due_at": order.due_at.isoformat(),
                    "priority_weight": order.priority_weight,
                    "hard_deadline": order.hard_deadline,
                },
            ).status_code
            == 200
        )
    if scenario == "order_due":
        order = source[2].orders[0]
        # A date moved to half an hour from now cannot be kept; the study proposes one instead.
        assert (
            control(
                source,
                "customer-rush",
                "order.revise",
                {
                    "order_id": order.order_id,
                    "expected_version": order.version,
                    "quantity": order.quantity,
                    "due_at": (snapshot(source).snapshot_clock + timedelta(minutes=30)).isoformat(),
                    "priority_weight": order.priority_weight,
                    "hard_deadline": order.hard_deadline,
                },
            ).status_code
            == 200
        )
    if scenario in {"immediate", "standard", "clock_race"}:
        stock = source[2].inventory[0]
        assert (
            control(
                source,
                "short-stock",
                "inventory.reconcile",
                {
                    "material_id": stock.material_id,
                    "expected_version": stock.version,
                    "counted_on_hand": stock.reserved,
                    "reason": "COUNT_CORRECTION",
                },
            ).status_code
            == 200
        )
        for receipt in source[2].receipts:
            if receipt.material_id == stock.material_id and receipt.status in {
                "CONFIRMED",
                "EXPECTED",
            }:
                assert (
                    control(
                        source,
                        "cancel-" + receipt.receipt_id,
                        "receipt.cancel",
                        {"receipt_id": receipt.receipt_id},
                    ).status_code
                    == 200
                )
    elif scenario == "staff":
        assert (
            control(
                source,
                "absent-person",
                "worker.absent",
                {"worker_id": source[2].workers[0].worker_id},
            ).status_code
            == 200
        )
    elif scenario == "repair":
        assert (
            control(
                source,
                "broken-machine",
                "resource.down",
                {"resource_id": source[2].resources[0].resource_id},
            ).status_code
            == 200
        )
    synchronize(engine, reader, factory)
    with Session(engine) as db, db.begin():
        db.execute(
            delete(Membership).where(
                Membership.user_id == actor.user_id,
                Membership.factory_id == factory,
                Membership.role.not_in(("manager", "planner")),
            )
        )
    actor = actor.model_copy(
        update={"grants": tuple(g for g in actor.grants if g.role in {"manager", "planner"})}
    )
    ctx = (*ctx[:3], actor, *ctx[4:])
    job = request_business_study(
        engine,
        actor,
        factory,
        request_id="treatment-study",
        request=BusinessStudyRequest(
            kind="production_exception",
            total_time_limit=6,
            existing_order_id=source[2].orders[0].order_id
            if scenario.startswith("order_")
            else None,
        ),
        case_id=case["case_id"],
    )
    assert run_once(engine)
    with Session(engine) as db:
        saved = db.get(SolveJob, job.job_id)
        assert saved.state == "SUCCEEDED", saved.error_code
        document = saved.business_result
        option = next(
            o
            for o in document["options"]
            if o["status"] == "FEASIBLE"
            and not o["allow_overtime"]
            and (
                scenario in {"normal", "clock_only", "changed_facts"}
                or any(
                    a["mode"] == ("standard" if scenario == "clock_race" else scenario)
                    if scenario in {"immediate", "standard", "clock_race"}
                    else a["kind"] == scenario
                    for a in o["actions"]
                )
            )
        )
    if scenario == "clock_only":
        assert control(source, "advance-clock", "clock.step", {"minutes": 1}).status_code == 200
        current = synchronize(engine, reader, factory)
        with Session(engine) as db:
            assert study_view(db.get(SolveJob, job.job_id), current)["current"]
    if scenario == "changed_facts":
        order = source[2].orders[0]
        assert (
            control(
                source,
                "late-demand-change",
                "order.revise",
                {
                    "order_id": order.order_id,
                    "expected_version": order.version,
                    "quantity": order.quantity * 2,
                    "due_at": order.due_at.isoformat(),
                    "priority_weight": order.priority_weight,
                    "hard_deadline": order.hard_deadline,
                },
            ).status_code
            == 200
        )
        synchronize(engine, reader, factory)
        with pytest.raises(AccessError, match="business facts"):
            request(
                ctx,
                "treatment_execute",
                {
                    "job_id": job.job_id,
                    "option_id": option["option_id"],
                    "study_hash": canonical_hash(document),
                },
            )
        return
    chosen = request(
        ctx,
        "treatment_execute",
        {
            "job_id": job.job_id,
            "option_id": option["option_id"],
            "study_hash": canonical_hash(document),
            "accept_customer_change": scenario.startswith("order_"),
        },
    )

    class LostAcknowledgement:
        lost = 0

        def command(self, factory_id, command):
            result = controls.command(factory_id, command)
            if command.kind == "treatment.apply" and self.lost < (
                3 if scenario == "immediate" else 1
            ):
                self.lost += 1
                raise ConnectorError("simulated lost acknowledgement")
            return result

    unreliable = LostAcknowledgement()
    for step in range(10):
        with Session(engine) as db, db.begin():
            db.execute(
                update(AssistantAction)
                .where(AssistantAction.action_id == chosen["action_id"])
                .values(next_attempt_at=datetime.now(UTC))
            )
        assert process_treatment(engine, reader, unreliable)
        if scenario == "clock_race" and step == 0:
            # Production moves on between checking and applying the approved measures.
            assert control(source, "race-tick", "clock.step", {"minutes": 1}).status_code == 200
        if cancel and step == 0:
            before = snapshot(source)
            cancelled = decide_execution(
                engine,
                controls,
                actor,
                factory,
                chosen["action_id"],
                ExecutionDecision(request_id="cancel-plan", decision="cancel"),
            )
            assert cancelled["state"] == "CANCELLED"
            if option["actions"]:
                late = controls.command(
                    factory, SimulatorCommand.model_validate(cancelled["result"]["source_command"])
                )
                assert late["cancelled"]
            assert snapshot(source).content_hash == before.content_hash
            return
        run_once(engine)
        deliver_one(engine, reader, writer)
        with Session(engine) as db:
            saved = db.get(AssistantAction, chosen["action_id"])
            if (
                saved.state == "ATTENTION"
                and scenario == "immediate"
                and saved.result.get("can_resume")
            ):
                decide_execution(
                    engine,
                    controls,
                    actor,
                    factory,
                    chosen["action_id"],
                    ExecutionDecision(request_id="resume-original-plan", decision="resume"),
                )
                continue
            assert saved.state != "ATTENTION", saved.result
            if saved.state == "DONE":
                break
    assert saved.state == "DONE"
    assert saved.result.get("refreshed", 0) == (1 if scenario == "clock_race" else 0)
    assert snapshot(source).active_plan_hash is not None
    with Session(engine) as db:
        completed_case = db.get(CaseRecord, case["case_id"])
        assert saved.result["candidate_id"] in completed_case.context["candidate_ids"]
        assert completed_case.context["last_execution"]["state"] == "DONE"
        done = db.scalar(
            select(CaseInput).where(
                CaseInput.case_id == case["case_id"], CaseInput.kind == "EXECUTION_RESULT"
            )
        )
        assert done is not None
        # The Agent reports the executed option from facts instead of re-asking for approval.
        reply = _execution_reply(db, synchronize(engine, reader, factory), [done])
        assert reply is not None
        message = json.loads(reply)["parameters"]["message"]
        assert option["title"] in message and "Executed the plan you approved" in message
    if option["actions"]:
        assert unreliable.lost
        with Session(source[4]) as db:
            effects = list(
                db.scalars(
                    select(SourceAction).where(
                        SourceAction.factory_id == factory,
                        SourceAction.operation_id == "treatment:" + chosen["action_id"],
                    )
                )
            )
            assert len(effects) == 1
    with Session(engine) as db:
        jobs = list(
            db.scalars(
                select(SolveJob).where(
                    SolveJob.request_id == "treatment-plan:" + chosen["action_id"]
                )
            )
        )
        assert len(jobs) == 1
