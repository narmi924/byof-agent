"""Independent solver process; leases fence abandoned/restarted workers."""

import argparse
import time

from sqlalchemy.orm import Session

from packages.domain.models import Snapshot
from packages.persistence import connect
from packages.planning.preferences import load_objective
from packages.planning.service import active_baseline, claim_job, complete_job
from packages.planning.store import SnapshotRecord
from packages.settings import Settings


def run_once(engine) -> bool:
    from packages.planning.solver import PlanningInputError, solve

    claimed = claim_job(engine)
    if claimed is None:
        return False
    try:
        with Session(engine) as db:
            record = db.get(SnapshotRecord, claimed.snapshot_id)
            if record is None:
                raise ValueError("Missing claimed snapshot")
            snapshot = Snapshot.model_validate(record.document)
            baseline = active_baseline(db, snapshot)
            if claimed.business_request is not None:
                from packages.domain.business_options import BusinessStudyRequest
                from packages.planning.business_options import evaluate_business_options
                from packages.planning.business_service import complete_business_study

                result_study = evaluate_business_options(
                    snapshot,
                    baseline,
                    BusinessStudyRequest.model_validate(claimed.business_request),
                    expedite_quotes=(
                        snapshot.business_terms.expedite_quotes if snapshot.business_terms else ()
                    ),
                )
                complete_business_study(engine, claimed, result_study)
                return True
            objective = load_objective(db, claimed.factory_id, claimed.objective_version)
        result = solve(
            snapshot,
            time_limit=claimed.time_limit,
            allow_overtime=claimed.allow_overtime,
            baseline=baseline,
            objective=objective,
            new_actions_not_before=claimed.new_actions_not_before,
        )
        complete_job(engine, claimed, result)
    except PlanningInputError as exc:
        complete_job(engine, claimed, None, exc.code)
    except Exception:
        # Store a bounded error code. Inputs, credentials and full model responses never enter logs.
        complete_job(engine, claimed, None, "SOLVER_FAILURE")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    engine = connect(Settings().database_url.get_secret_value())
    try:
        if args.once:
            run_once(engine)
        else:
            while True:
                if not run_once(engine):
                    time.sleep(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
