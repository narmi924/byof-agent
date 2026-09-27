"""Verified opening plans for untouched demonstration days.

A preset is an offline CP-SAT schedule for one scenario's opening facts, stored in minutes
relative to the planning horizon. It is used only when today's facts match that opening state
exactly after the date shift; the result is rebuilt and independently checked like any solve.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from packages.domain.models import Assignment, Snapshot

PRESET_FILE = Path(__file__).resolve().parents[2] / "database/presets/day-plans.json"
PRESET_SCHEMA = "byof.day-presets/1"
# Identity and bookkeeping that differ between runs of the same opening facts.
_VOLATILE = {"run_id", "snapshot_id", "content_hash", "source", "version", "planning_revision"}
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")


def _relative(value: Any, origin: datetime) -> Any:
    if isinstance(value, dict):
        return {k: _relative(v, origin) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_relative(item, origin) for item in value]
    if isinstance(value, str) and _TIMESTAMP.match(value):
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return {"minutes": (moment - origin) / timedelta(minutes=1)}
    return value


def opening_fingerprint(snapshot: Snapshot) -> str:
    """Digest of the planning facts with dates expressed relative to the horizon start."""
    data = _relative(snapshot.model_dump(mode="json"), snapshot.horizon.start_at)
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def preset_entry(snapshot: Snapshot, assignments: tuple[Assignment, ...], **meta: Any) -> dict:
    origin = snapshot.horizon.start_at

    def offset(moment: datetime) -> int:
        return int((moment - origin) / timedelta(minutes=1))

    return {
        "fingerprint": opening_fingerprint(snapshot),
        **meta,
        "assignments": [
            {
                "operation_id": a.operation_id,
                "resource_id": a.resource_id,
                "worker_id": a.worker_id,
                "changeover_start": offset(a.changeover_start),
                "start_at": offset(a.start_at),
                "end_at": offset(a.end_at),
            }
            for a in assignments
        ],
    }


@cache
def _presets(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != PRESET_SCHEMA:
        return {}
    return {entry["fingerprint"]: entry for entry in document["presets"]}


def opening_preset(
    snapshot: Snapshot, path: Path | None = None
) -> tuple[dict, tuple[Assignment, ...]] | None:
    """Return the matching preset shifted onto this run, or None when facts differ."""
    if snapshot.actuals or snapshot.active_plan_version is not None:
        return None
    if snapshot.snapshot_clock != snapshot.horizon.start_at:
        return None
    entry = _presets(path or PRESET_FILE).get(opening_fingerprint(snapshot))
    if entry is None:
        return None
    origin = snapshot.horizon.start_at

    def moment(minutes: int) -> datetime:
        return origin + timedelta(minutes=minutes)

    assignments = tuple(
        Assignment(
            operation_id=item["operation_id"],
            resource_id=item["resource_id"],
            worker_id=item["worker_id"],
            changeover_start=moment(item["changeover_start"]),
            start_at=moment(item["start_at"]),
            end_at=moment(item["end_at"]),
        )
        for item in entry["assignments"]
    )
    return entry, assignments
