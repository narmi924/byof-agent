"""Bound model input without rewriting evidence or discarding decision-critical fields."""

import json
from copy import deepcopy

from packages.domain.models import canonical_hash

MAX_CONTEXT_CHARACTERS = 110_000
COLLECTIONS = ("orders", "operations", "resources", "workers", "materials")


class ContextBudgetExceeded(ValueError):
    """Critical context alone exceeds the budget; the model must not receive a partial account."""


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _identities(values: object, limit: int, reference: dict) -> object:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        return values
    if len(values) <= limit:
        return values
    summary = {
        "items": values[:limit],
        "total": len(values),
        "included": limit,
        "omitted": len(values) - limit,
        "truncated": True,
        "content_hash": canonical_hash({"identities": values}),
        "reference": reference,
    }
    return summary if _size(summary) < _size(values) else values


def _project(context: dict, limit: int) -> dict:
    projected = deepcopy(context)
    case = projected.get("case", {})
    case_id = case.get("case_id")
    reports: dict[str, dict] = {}
    locations: dict[str, list[dict]] = {}
    report_count = 0

    def impact(container: dict, reference: dict) -> None:
        nonlocal report_count
        original = container.get("impact")
        if not isinstance(original, dict):
            return
        digest = canonical_hash(original)
        report_count += 1
        locations.setdefault(digest, []).append(reference)
        if digest not in reports:
            summary = deepcopy(original)
            for area in ("direct", "possible"):
                scope = summary.get(area)
                if not isinstance(scope, dict):
                    continue
                for name in COLLECTIONS:
                    if name in scope:
                        scope[name] = _identities(
                            scope[name],
                            limit,
                            {"report_hash": digest, "field": f"{area}.{name}"},
                        )
            if "dependency_operations" in summary:
                summary["dependency_operations"] = _identities(
                    summary["dependency_operations"],
                    limit,
                    {"report_hash": digest, "field": "dependency_operations"},
                )
            # Classification, unknowns, version anchors and non-proven delay remain verbatim.
            reports[digest] = summary
        container["impact"] = {"report_ref": digest}

    details = case.get("context")
    if isinstance(details, dict):
        impact(details, {"case_id": case_id, "field": "context.impact"})
    for item in projected.get("inputs", []):
        payload = item.get("data")
        if not isinstance(payload, dict):
            continue
        reference = {"case_id": case_id, "input_id": item.get("id")}
        impact(payload, {**reference, "field": "payload.impact"})
        if "event_ids" in payload:
            payload["event_ids"] = _identities(
                payload["event_ids"], limit, {**reference, "field": "payload.event_ids"}
            )
    projected["impact_reports"] = reports
    projected["context_projection"] = {
        "schema_version": "byof.model-context-projection/1",
        "truncated": True,
        "report_occurrences": report_count,
        "distinct_reports": len(reports),
        "report_storage_references": locations,
        "id_sample_limit": limit,
        "notes": [
            "impact.report_ref refers to the full classification and version summary in impact_reports of this context.",
            "Objects marked truncated list only some IDs; total is the size of the original set, and omitted objects are not unaffected.",
            "The database keeps the full reports and inputs; references are for checks in the authorized workbench and are not new tools or access.",
            "query can page through the current orders, inventory, receipts, resources, workers and actuals; "
            "at most 50 per page, continuing from the returned next_offset. policy can be queried.",
            "actuals only contain execution records that happened; the current query does not page unstarted operations or historical reports. "
            "For a full schedule use the existing solve_scenario and rely on the independent Checker result; never infer feasibility from ID samples.",
            "Current facts follow the facts version; old versions cited by historical reports never replace this turn's facts.",
        ],
    }
    return projected


def project_context(context: dict, *, max_characters: int = MAX_CONTEXT_CHARACTERS) -> dict:
    """Return an isolated projection; non-ID facts are never shortened to force a model call."""
    if type(max_characters) is not int or max_characters <= 0:
        raise ValueError("Context budget must be a positive integer")
    if _size(context) <= max_characters:
        return deepcopy(context)
    for sample_limit in (16, 8, 4, 1, 0):
        projected = _project(context, sample_limit)
        if _size(projected) <= max_characters:
            return projected
    raise ContextBudgetExceeded("Critical context exceeds the model input budget")
