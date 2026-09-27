"""Comparable portfolio contribution; missing evidence never becomes zero profit."""

from datetime import timedelta

from packages.domain.business_options import BusinessOption, BusinessStudyRequest
from packages.domain.economics import (
    OVERTIME_PREMIUM_PER_MINUTE,
    PRODUCT_RATES,
    EconomicLine,
    EconomicSensitivity,
    OptionEconomics,
    late_deduction,
)
from packages.domain.models import Snapshot
from packages.domain.treatment import action_cost

ASSUMPTIONS = (
    "SGD rates come from the price catalog; product prices and regular variable costs are fixed by the catalog and are not settled accounting profit.",
    "Assumes full fulfilment of all open orders in this comparison; regular variable cost already includes material and normal labor, so extra purchases only add premiums and freight.",
    "Tardiness deducts 1% of sales per day (prorated by minute, up to 20%).",
    "New cash counts only the extra money the option spends; net contribution is calculated separately.",
)


def estimate(snapshot: Snapshot, option: BusinessOption) -> OptionEconomics:
    if option.status != "FEASIBLE" or option.candidate is None or option.derived_snapshot is None:
        return OptionEconomics(
            status="INCOMPLETE",
            assumptions=ASSUMPTIONS,
            missing=(
                "No fully delivered schedule has passed the independent check yet, so the achievable net contribution cannot be computed.",
            ),
        )
    orders = {o.order_id: o for o in option.derived_snapshot.orders}
    missing = []
    lines: list[EconomicLine] = []
    revenue = variable = penalty = adverse_penalty = 0
    for impact in option.impacts:
        order = orders.get(impact.order_id)
        if order is None:
            missing.append(f"Order {impact.order_id} has no matching business terms.")
            continue
        if order.status in {"COMPLETED", "CANCELLED"}:
            continue
        rates = PRODUCT_RATES.get(order.product_id)
        if rates is None or impact.tardiness_minutes is None:
            missing.append(
                f"Order {impact.order_id} is missing a catalog price or a full delivery time."
            )
            continue
        value, cost = impact.quantity * rates[0], impact.quantity * rates[1]
        deduction = late_deduction(value, impact.tardiness_minutes)
        revenue += value
        variable += cost
        penalty += deduction
        adverse_penalty += late_deduction(value, impact.tardiness_minutes + 60)
        lines.extend(
            (
                EconomicLine(
                    label=f"{impact.order_id} regular variable cost",
                    amount_minor=cost,
                    basis=f"{impact.quantity} pcs × {rates[1]} SGD cents/pc, including regular material and labor",
                ),
                EconomicLine(
                    label=f"{impact.order_id} tardiness deduction",
                    amount_minor=deduction,
                    basis=f"Sales {value} SGD cents; {impact.tardiness_minutes} min late",
                ),
            )
        )
    future_overtime = timedelta(0)
    workers = {w.worker_id: w for w in option.derived_snapshot.workers}
    completed = {a.operation_id for a in snapshot.actuals if a.state == "COMPLETED"}
    for assignment in option.candidate.assignments:
        if assignment.operation_id in completed:
            continue
        begin = max(
            snapshot.snapshot_clock,
            assignment.resume_changeover_start or assignment.changeover_start,
        )
        for window in workers[assignment.worker_id].calendar:
            if window.kind == "OVERTIME":
                future_overtime += max(
                    timedelta(0),
                    min(assignment.end_at, window.end_at) - max(begin, window.start_at),
                )
    minutes = -(-future_overtime // timedelta(minutes=1))
    extra = minutes * OVERTIME_PREMIUM_PER_MINUTE
    cash = extra
    lines.append(
        EconomicLine(
            label="Added overtime premium",
            amount_minor=extra,
            basis=f"{minutes} staff-min of unrun overtime × {OVERTIME_PREMIUM_PER_MINUTE} SGD cents/staff-min",
        )
    )
    if option.quote_id:
        if option.cost_minor is None or option.currency != "SGD":
            missing.append(
                "An expedite quote has no price or a different currency; no exchange rate is assumed and it is not added up as zero cost."
            )
        else:
            extra += option.cost_minor
            cash += option.cost_minor
            lines.append(
                EconomicLine(
                    label="Receipt expedite surcharge",
                    amount_minor=option.cost_minor,
                    basis="Surcharge of the selected source quote; regular material cost is not counted twice",
                )
            )
    for action in option.actions:
        expense, outlay = action_cost(action)
        extra += expense
        cash += outlay
        lines.append(
            EconomicLine(
                label=f"{action.target_id} handling surcharge",
                amount_minor=expense,
                basis=f"Price catalog; {action.kind} / {action.mode}; quantity {action.quantity}; new cash {outlay} SGD cents",
            )
        )
    if missing:
        return OptionEconomics(
            status="INCOMPLETE", assumptions=ASSUMPTIONS, missing=tuple(missing), lines=tuple(lines)
        )
    return OptionEconomics(
        status="ESTIMATED",
        revenue_minor=revenue,
        variable_cost_minor=variable,
        additional_cost_minor=extra,
        late_deduction_minor=penalty,
        net_contribution_minor=revenue - variable - extra - penalty,
        incremental_cash_outlay_minor=cash,
        lines=tuple(lines),
        assumptions=ASSUMPTIONS,
        adverse=EconomicSensitivity(
            net_contribution_minor=revenue - variable - extra - (extra + 4) // 5 - adverse_penalty,
            incremental_cash_outlay_minor=cash + (extra + 4) // 5,
            basis="Sensitivity case: the financial result if handling surcharges and overtime premiums rise by 20% and every open order finishes 60 minutes later (costs recalculated only, not rescheduled). Exceeding the approved amount or impact needs a new approval.",
        ),
    )


def with_economics(snapshot: Snapshot, options: list[BusinessOption]) -> list[BusinessOption]:
    estimated = [(option, estimate(snapshot, option)) for option in options]
    reference = next(
        (
            (option, economics)
            for option, economics in estimated
            if option.kind in {"normal", "shared_material"}
            and not option.diagnostic_only
            and economics.net_contribution_minor is not None
        ),
        None,
    )
    result = []
    for option, economics in estimated:
        if reference is not None and economics.net_contribution_minor is not None:
            amount = reference[1].net_contribution_minor
            assert amount is not None
            economics = economics.model_copy(
                update={
                    "improvement_minor": economics.net_contribution_minor - amount,
                    "comparison_option_id": reference[0].option_id,
                }
            )
        result.append(option.model_copy(update={"economics": economics}))
    return result


def rank_treatments(
    options: list[BusinessOption], request: BusinessStudyRequest, source: Snapshot | None = None
) -> list[BusinessOption]:
    """Rank checked alternatives; no claim of a global optimum outside this search.

    Delivering fewer units is its own cost: a reduced order never dominates a full delivery.
    """
    ordered = {o.order_id: o.quantity for o in source.orders} if source else {}
    checked = []
    for option in options:
        economics = option.economics
        if (
            request.max_cash_outlay_minor is not None
            and economics is not None
            and economics.incremental_cash_outlay_minor is not None
            and economics.incremental_cash_outlay_minor > request.max_cash_outlay_minor
        ):
            option = option.model_copy(
                update={
                    "status": "BLOCKED",
                    "summary": "Exceeds the new cash limit set by the manager; the comparison basis is kept, but it cannot be approved for execution.",
                }
            )
        checked.append(option)

    def metrics(option: BusinessOption) -> tuple[int, int, int, int, int, int]:
        assert option.economics and option.economics.net_contribution_minor is not None
        assert option.economics.incremental_cash_outlay_minor is not None
        return (
            -option.economics.net_contribution_minor,
            option.economics.incremental_cash_outlay_minor,
            sum((impact.tardiness_minutes or 0) * impact.quantity for impact in option.impacts),
            option.changed_operations if option.changed_operations is not None else 2**63,
            option.incremental_overtime_minutes
            if option.incremental_overtime_minutes is not None
            else 2**63,
            sum(
                max(0, ordered.get(action.target_id, action.quantity) - action.quantity)
                for action in option.actions
                if action.kind == "order_quantity"
            ),
        )

    feasible = [
        option
        for option in checked
        if option.status == "FEASIBLE"
        and not option.diagnostic_only
        and option.economics
        and option.economics.status == "ESTIMATED"
    ]

    dominated_ids = {
        option.option_id
        for option in feasible
        if any(
            all(a <= b for a, b in zip(metrics(other), metrics(option), strict=True))
            and metrics(other) != metrics(option)
            for other in feasible
        )
    }

    def order(option: BusinessOption) -> tuple:
        values = metrics(option)
        preferred = {
            "cash": values[1],
            "delivery": values[5] * 2**40 + values[2],  # fewer units short first, then delay
            "stability": values[3],
            "overtime": values[4],
        }.get(request.economic_priority or "contribution", values[0])
        return (
            option.option_id in dominated_ids,
            preferred,
            *values,
            len(option.actions),
            option.option_id,
        )

    ranked = [
        option.model_copy(update={"dominated": option.option_id in dominated_ids})
        for option in sorted(feasible, key=order)
    ]
    identities = {option.option_id for option in ranked}
    return [*ranked, *(option for option in checked if option.option_id not in identities)]
