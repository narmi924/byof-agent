"""Business facts cannot turn an intermediate step or an old plan into a promise."""

from test_checker import at, example_assignments, example_snapshot, make_candidate

from packages.agent.decisions import ProductionReportAction, ReplyParameters, parse_action
from packages.domain.models import ActualExecution
from packages.domain.production_facts import order_facts, plan_review, production_brief
from packages.domain.skf import load_skf_snapshot


def test_old_stock_and_generated_reply_cannot_reenter_current_fact_context():
    from packages.agent.planning_context import current_tool_result

    previous = {
        "snapshot_hash": "before",
        "items": [{"on_hand": 1}],
        "summary": "Still short by 49 pcs",
    }
    for action in ("query", "reply", "report_production"):
        projected = current_tool_result(action, previous, "after")
        assert projected["status"] == "HISTORICAL"
        assert "items" not in projected and "49" not in projected["summary"]
        assert current_tool_result(action, previous, "before") == previous
    assert previous["items"] == [{"on_hand": 1}]  # Durable audit record stays intact.
    rejection = {"status": "REJECTED", "code": "OBJECT_NOT_FOUND"}
    assert current_tool_result("query", rejection, "after") == rejection


def completed(code, *, quality="PASSED", minute=2):
    return ActualExecution(
        operation_id=f"order-a-R001-B001-{code}",
        batch_id="order-a-R001-B001",
        route_version="r1",
        state="COMPLETED",
        actual_start=at(0),
        actual_end=at(minute),
        changeover_start=at(0),
        resource_id="r1",
        worker_id="w1",
        completed_quantity=2,
        quality_state=quality,
        version=1,
    )


def test_capacity_followup_ignores_normal_setup_progress_but_observes_restoration():
    from packages.agent.recovery_followup import followup_key

    source = example_snapshot()
    progress = source.model_copy(
        update={
            "resources": tuple(
                r.model_copy(
                    update={
                        "last_operation_id": "finished-step",
                        "last_product_id": "product-a",
                        "version": r.version + 1,
                    }
                )
                for r in source.resources
            )
        }
    )
    outage = source.model_copy(
        update={
            "resources": tuple(r.model_copy(update={"status": "DOWN"}) for r in source.resources)
        }
    )
    for kind in (
        "equipment_recovery",
        "workforce_recovery",
        "capacity_window_recovery",
        "commitment_recovery",
    ):
        assert followup_key(source, kind) == followup_key(progress, kind)
        assert followup_key(source, kind) != followup_key(outage, kind)


def test_intermediate_completion_is_not_finished_goods_and_quality_is_required():
    source = example_snapshot()
    partial = source.model_copy(update={"actuals": (completed("begin"),)})
    assert order_facts(partial)[0]["qualified_completed_quantity"] == 0
    assert order_facts(partial)[0]["in_progress_quantity"] == 2
    all_steps = tuple(completed(code) for code in ("begin", "check", "finish"))
    complete = source.model_copy(update={"actuals": all_steps})
    assert order_facts(complete)[0]["qualified_completed_quantity"] == 2
    failed = complete.model_copy(
        update={"actuals": (*all_steps[:2], completed("finish", quality="FAILED"))}
    )
    assert order_facts(failed)[0]["qualified_completed_quantity"] == 0


def test_old_plan_coverage_does_not_expand_with_new_order_quantity():
    source = example_snapshot()
    plan = make_candidate(source, example_assignments())
    changed = source.model_copy(
        update={"orders": (source.orders[0].model_copy(update={"quantity": 4}),)}
    )
    facts = order_facts(changed, plan)[0]
    assert facts["quantity"] == 4 and facts["plan_covered_quantity"] == 2
    assert facts["uncovered_quantity"] == 2 and facts["planned_completion_at"] is None
    assert facts["forecast_requires_revalidation"]


def test_6204_shortage_does_not_mark_other_skus_as_directly_short():
    source = load_skf_snapshot()
    changed = source.model_copy(
        update={
            "orders": tuple(
                o.model_copy(update={"quantity": 2000})
                if o.order_id == "SO-003"
                else o.model_copy(update={"quantity": 0, "status": "CANCELLED"})
                if o.order_id == "SO-002"
                else o
                for o in source.orders
            )
        }
    )
    facts = {o["order_id"]: o for o in order_facts(changed)}
    assert len(facts["SO-003"]["direct_shortage_materials"]) == 6
    assert facts["SO-003"]["material_quantity_upper_bound"] == 1200
    assert all(not facts[o]["direct_shortage_materials"] for o in ("SO-004", "SO-005", "SO-006"))
    assert facts["SO-003"]["due_at"].endswith("17:30:00+08:00")
    # The Agent answers from plain facts: the order in question, its watch points, the gaps.
    brief = production_brief(changed, None, "SO-003")
    assert [o["order_id"] for o in brief["orders"]] == ["SO-003"]
    assert brief["orders"][0]["watch"] and "Materials short" in brief["orders"][0]["watch"][-1]
    assert brief["material_gaps"] and not brief["plan_in_effect"]
    assert all("does not" not in text for text in brief["material_gaps"])


def test_report_action_and_long_business_reply_have_separate_limits():
    import json

    assert isinstance(
        parse_action(
            json.dumps(
                {
                    "action": "report_production",
                    "parameters": {},
                    "reason_summary": "Check order delivery.",
                }
            )
        ),
        ProductionReportAction,
    )
    assert len(ReplyParameters(message="note" * 200).message) == 800


def test_review_exposes_order_changes_and_specific_overtime():
    source = example_snapshot()
    old = make_candidate(source, example_assignments())
    new = make_candidate(source, example_assignments(shift=30))
    review = plan_review(source, new, old)
    assert (
        review["orders"][0]["previous_completion_at"]
        != review["orders"][0]["planned_completion_at"]
    )
    assert review["orders"][0]["planned_on_time_quantity"] == 0
    assert {o["worker_id"] for o in review["overtime"]}
