import json
from datetime import datetime, timedelta

from packages.domain.models import Snapshot
from packages.domain.skf import load_skf_snapshot
from packages.planning import presets
from packages.planning import solver as planning
from packages.planning.dispatch import dispatch


def shifted(snapshot: Snapshot, days: int) -> Snapshot:
    """The same opening facts on a later day, as a new demonstration run would present them."""
    data = snapshot.model_dump(mode="json", exclude={"content_hash"})

    def move(value):
        if isinstance(value, dict):
            return {k: move(v) for k, v in value.items()}
        if isinstance(value, list):
            return [move(v) for v in value]
        if isinstance(value, str) and presets._TIMESTAMP.match(value):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (moment + timedelta(days=days)).isoformat()
        return value

    data = move(data)
    data["snapshot_id"] = f"{snapshot.snapshot_id}-later"
    return Snapshot.model_validate(data)


def write_preset(tmp_path, monkeypatch, snapshot: Snapshot) -> None:
    candidate = planning.solve(snapshot, time_limit=10)
    assert candidate.checker.status == "PASS"
    path = tmp_path / "day-plans.json"
    entry = presets.preset_entry(snapshot, candidate.assignments, scenario="test")
    path.write_text(json.dumps({"schema": presets.PRESET_SCHEMA, "presets": [entry]}))
    monkeypatch.setattr(presets, "PRESET_FILE", path)
    presets._presets.cache_clear()


def test_untouched_opening_uses_checked_preset_on_a_later_day(tmp_path, monkeypatch):
    opening = load_skf_snapshot(development=True)
    write_preset(tmp_path, monkeypatch, opening)
    later = shifted(opening, 3)
    candidate, evidence = planning.solve_with_evidence(later, time_limit=10)
    assert evidence["solver"] == "opening-preset"
    assert candidate.checker.status == "PASS"
    assert candidate.has_solution and candidate.proven_objective_levels == 0
    assert all(metric.lower_bound is None for metric in candidate.objective)
    assert min(a.start_at for a in candidate.assignments) >= later.snapshot_clock


def test_changed_facts_or_review_delay_fall_back_to_solving(tmp_path, monkeypatch):
    opening = load_skf_snapshot(development=True)
    write_preset(tmp_path, monkeypatch, opening)
    data = opening.model_dump(mode="json", exclude={"content_hash"})
    data["inventory"][0]["on_hand"] += 1
    changed = Snapshot.model_validate(data)
    _, evidence = planning.solve_with_evidence(changed, time_limit=10)
    assert evidence["solver"] == "OR-Tools CP-SAT"
    delay = opening.snapshot_clock + timedelta(minutes=15)
    candidate, evidence = planning.solve_with_evidence(
        opening, time_limit=10, new_actions_not_before=delay
    )
    assert evidence["solver"] == "OR-Tools CP-SAT"
    assert candidate.checker.status == "PASS"


def test_opening_hint_respects_review_delay():
    opening = load_skf_snapshot(development=True)
    delay = opening.snapshot_clock + timedelta(minutes=15)
    hint = dispatch(opening, new_actions_not_before=delay)
    assert hint and min(a.changeover_start for a in hint) >= delay
