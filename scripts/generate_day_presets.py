"""Generate verified opening-day presets for the workshop scenarios.

Each preset is solved offline with a longer patience, checked independently, and stored in
minutes relative to the planning horizon. Runtime use still rebuilds and checks the plan.

    uv run --locked python -m scripts.generate_day_presets
"""

import argparse
import json
import time

from packages.domain.models import Snapshot
from packages.planning import solver
from packages.planning.checker import check_candidate
from packages.planning.presets import PRESET_FILE, PRESET_SCHEMA, preset_entry
from scripts.setup_team import WORKSHOP_SAFETY_STOCK, workshop_snapshot


def scenarios() -> dict[str, Snapshot]:
    current = workshop_snapshot()
    # workshop-full-1 is the earlier opening state without the declared 6204 safety stock.
    earlier = current.model_dump(mode="json", exclude={"content_hash"})
    earlier["profile"]["version"] = "V1.6.workshop-full-1"
    for stock in earlier["inventory"]:
        stock["on_hand"] -= WORKSHOP_SAFETY_STOCK.get(stock["material_id"], 0)
    return {"workshop-full-2": current, "workshop-full-1": Snapshot.model_validate(earlier)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--patience", type=float, default=15.0)
    args = parser.parse_args()
    solver.IMPROVEMENT_PATIENCE_SECONDS = args.patience
    # Never reuse an existing preset while generating one.
    solver._preset_candidate = lambda *_args, **_kwargs: None
    presets = []
    for name, snapshot in scenarios().items():
        started = time.perf_counter()
        candidate, _ = solver.solve_with_evidence(snapshot, time_limit=args.seconds)
        report = check_candidate(snapshot, candidate)
        if not candidate.has_solution or report.status != "PASS":
            raise SystemExit(f"{name}: no checked opening plan ({candidate.native_status})")
        metrics = {m.name: m.value for m in candidate.objective}
        presets.append(
            preset_entry(
                snapshot,
                candidate.assignments,
                scenario=name,
                profile_version=snapshot.profile.version,
                generated_with={
                    "solver": "OR-Tools CP-SAT",
                    "seed": solver.SEED,
                    "time_limit_seconds": args.seconds,
                    "patience_seconds": args.patience,
                    "native_status": candidate.native_status,
                },
                metrics=metrics,
            )
        )
        print(
            f"{name}: {candidate.native_status} {metrics} in {time.perf_counter() - started:.1f}s"
        )
    PRESET_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRESET_FILE.write_text(
        json.dumps({"schema": PRESET_SCHEMA, "presets": presets}, ensure_ascii=False, indent=1)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {PRESET_FILE}")


if __name__ == "__main__":
    main()
