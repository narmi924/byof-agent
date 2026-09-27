"""Exercise local HTTP planning and conditional execution without model calls."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from packages.domain.models import Candidate, Snapshot, batch_operations
from packages.planning.checker import check_candidate

ROOT = Path(__file__).resolve().parents[1]


class SmokeFailure(RuntimeError):
    pass


def require(condition: object, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def local_origin(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("The container smoke test only sends credentials to a local HTTP origin")
    return value.rstrip("/")


def response_json(response: httpx.Response, expected: int = 200) -> dict:
    require(
        response.status_code == expected, f"HTTP_STATUS_{response.status_code}_EXPECTED_{expected}"
    )
    body = response.json()
    require(isinstance(body, dict), "HTTP_RESPONSE_MUST_BE_OBJECT")
    return body


def exercise(
    client: httpx.Client,
    *,
    seconds: int,
    timeout: float,
    execute: bool,
    evidence: dict,
) -> None:
    factory_id = "skf-workshop"
    prefix = f"/api/factories/{factory_id}"
    origin = str(client.base_url).rstrip("/")
    deadline = time.monotonic() + timeout
    evidence["stage"] = "readiness"
    while True:
        try:
            response = client.get("/health/ready", timeout=5)
            if response.status_code == 200:
                require(response_json(response).get("status") == "ok", "READINESS_PAYLOAD_INVALID")
                break
        except httpx.TransportError:
            pass
        require(time.monotonic() < deadline, "READINESS_TIMEOUT")
        time.sleep(1)
    page = client.get("/")
    require(page.status_code == 200 and 'id="root"' in page.text, "WORKBENCH_HTML_UNAVAILABLE")
    status = response_json(client.get("/api/system/status"))
    require(status.get("state") == "ready", "APPLICATION_NOT_READY")
    response_json(client.get("/api/session"), 401)

    evidence["stage"] = "role_selection_and_authorization"
    login = response_json(
        client.post(
            "/api/role-session",
            headers={"origin": origin},
            json={"role": "manager"},
        )
    )
    csrf = login.get("csrf_token")
    if not isinstance(csrf, str) or not csrf:
        raise SmokeFailure("LOGIN_MISSING_CSRF")
    headers = {"origin": origin, "x-csrf-token": csrf}
    actor = response_json(client.get("/api/session"))
    roles = {grant["role"] for grant in actor["grants"] if grant["factory_id"] == factory_id}
    require(roles == {"planner", "manager"}, "SMOKE_REQUIRES_MANAGER_ROLE")
    response_json(client.post(prefix + "/sync", headers={"origin": origin}), 403)
    response_json(client.get("/api/factories/not-authorized/workspace"), 403)
    evidence["checks"] = ["session_required", "csrf_required", "factory_scope"]

    evidence["stage"] = "source_sync"
    sync = response_json(client.post(prefix + "/sync", headers=headers))
    workspace = response_json(client.get(prefix + "/workspace"))
    snapshot = Snapshot.model_validate(workspace["snapshot"])
    require(snapshot.snapshot_id == sync["snapshot_id"], "SYNC_SNAPSHOT_ID_MISMATCH")
    require(snapshot.content_hash == sync["content_hash"], "SYNC_CONTENT_HASH_MISMATCH")
    require(
        snapshot.source.source_system == "factory-simulator-http-v1"
        and snapshot.source.ownership == "simulator_fact",
        "EXPECTED_HTTP_SIMULATOR_SOURCE",
    )
    batches, operations = batch_operations(snapshot)
    actual_counts = (
        len(snapshot.orders),
        sum(order.quantity for order in snapshot.orders),
        len(batches),
        len(operations),
    )
    require(actual_counts == (6, 5400, 108, 864), "INPUT_COUNTS_CHANGED")
    require(
        snapshot.active_plan_version is None and not snapshot.actuals, "EXPECTED_INITIAL_FACTORY"
    )
    evidence.update(snapshot_hash=snapshot.content_hash, input_counts=actual_counts)

    evidence["stage"] = "solve"
    solve_body = {
        "request_id": evidence["request_id"],
        "allow_overtime": False,
        "time_limit": seconds,
    }
    requested = response_json(client.post(prefix + "/solve", headers=headers, json=solve_body))
    duplicate = response_json(client.post(prefix + "/solve", headers=headers, json=solve_body))
    require(requested["job_id"] == duplicate["job_id"], "SOLVE_IDEMPOTENCY_FAILED")
    evidence["job_id"] = requested["job_id"]
    deadline = time.monotonic() + timeout + seconds
    while True:
        workspace = response_json(client.get(prefix + "/workspace"))
        matches = [job for job in workspace["jobs"] if job["job_id"] == requested["job_id"]]
        require(len(matches) == 1, "SOLVE_JOB_MISSING_OR_DUPLICATED")
        job = matches[0]
        require(job["state"] != "FAILED", "SOLVE_JOB_FAILED")
        if job["state"] == "SUCCEEDED":
            break
        require(time.monotonic() < deadline, "SOLVE_WORKER_TIMEOUT")
        time.sleep(1)
    matching_candidates = [
        entry
        for entry in workspace["candidates"]
        if entry["candidate"]["candidate_id"] == job["candidate_id"]
    ]
    require(len(matching_candidates) == 1, "SOLVE_CANDIDATE_MISSING_OR_DUPLICATED")
    entry = matching_candidates[0]
    candidate = Candidate.model_validate(entry["candidate"])
    require(candidate.has_solution, "SOLVER_RETURNED_NO_SOLUTION")
    require(candidate.checker.status == "PASS", "SERVER_CHECKER_REJECTED_PLAN")
    require(check_candidate(snapshot, candidate).status == "PASS", "CLIENT_CHECKER_REJECTED_PLAN")
    require(len(candidate.assignments) == len(operations), "ASSIGNMENTS_DO_NOT_COVER_SCOPE")
    require(not entry["approvals"] and entry["state"] == "CANDIDATE", "UNAPPROVED_CANDIDATE_STATE")
    evidence.update(
        native_status=candidate.native_status,
        checker=candidate.checker.status,
        candidate_id=candidate.candidate_id,
        candidate_hash=candidate.content_hash,
        operations=len(candidate.assignments),
        metrics=[metric.model_dump() for metric in candidate.objective],
        proven_objective_levels=candidate.proven_objective_levels,
    )
    evidence["checks"].extend(
        ["http_source_snapshot", "solve_idempotency", "full_scope", "independent_checker"]
    )

    evidence["stage"] = "approval"
    approval_path = prefix + f"/candidates/{candidate.candidate_id}/approvals"
    response_json(client.get(approval_path), 405)
    approval_body = {
        "request_id": evidence["approval_request_id"],
        "candidate_hash": candidate.content_hash,
        "action_scope": "publish_plan",
        "decision": "APPROVED",
    }
    unchanged = response_json(client.get(prefix + "/workspace"))
    pending = next(
        row
        for row in unchanged["candidates"]
        if row["candidate"]["candidate_id"] == candidate.candidate_id
    )
    require(
        not pending["approvals"] and pending["state"] == "CANDIDATE",
        "GET_CHANGED_APPROVALS",
    )
    approval = response_json(client.post(approval_path, headers=headers, json=approval_body))
    duplicate_approval = response_json(
        client.post(approval_path, headers=headers, json=approval_body)
    )
    require(
        approval["approval_id"] == duplicate_approval["approval_id"], "APPROVAL_IDEMPOTENCY_FAILED"
    )
    workspace = response_json(client.get(prefix + "/workspace"))
    approved = next(
        row
        for row in workspace["candidates"]
        if row["candidate"]["candidate_id"] == candidate.candidate_id
    )
    require(approved["state"] == "APPROVED", "APPROVAL_NOT_RECORDED")
    require(len(approved["approvals"]) == 1, "APPROVAL_DUPLICATED")
    require(
        approved["approvals"][0]["approver_role"] == "planner"
        and approved["approvals"][0]["candidate_hash"] == candidate.content_hash,
        "APPROVAL_SCOPE_OR_ROLE_MISMATCH",
    )
    require(
        workspace["snapshot"]["active_plan_version"] is None
        and not workspace["snapshot"]["actuals"]
        and approved["state"] != "ACTIVE",
        "APPROVAL_WAS_MISREPRESENTED_AS_FACTORY_EXECUTION",
    )
    evidence.update(
        approval_id=approval["approval_id"], final_state=approved["state"], real_model_requests=0
    )
    evidence["checks"].extend(
        [
            "get_has_no_approval_effect",
            "manager_review",
            "approval_idempotency",
            "approved_without_execution",
        ]
    )
    if execute:
        evidence["stage"] = "conditional_publication"
        publish_path = prefix + f"/candidates/{candidate.candidate_id}/publications"
        response_json(client.get(publish_path), 405)
        publish_body = {
            "request_id": evidence["publication_request_id"],
            "candidate_hash": candidate.content_hash,
        }
        local = response_json(client.post(publish_path, headers=headers, json=publish_body))
        require(local["local_state"] == "LOCAL_COMMITTED", "PUBLICATION_NOT_COMMITTED")
        duplicate = response_json(client.post(publish_path, headers=headers, json=publish_body))
        require(duplicate["release_id"] == local["release_id"], "PUBLICATION_NOT_IDEMPOTENT")
        deadline = time.monotonic() + timeout
        while True:
            workspace = response_json(client.get(prefix + "/workspace"))
            release = next(
                row["release"]
                for row in workspace["publications"]
                if row["release"]["release_id"] == local["release_id"]
            )
            require(release["source_state"] != "REJECTED", "SOURCE_REJECTED_PUBLICATION")
            if (
                release["source_state"] == "ACTIVE"
                and workspace["snapshot"]["active_plan_hash"] == candidate.content_hash
            ):
                break
            require(time.monotonic() < deadline, "SOURCE_ACCEPTANCE_TIMEOUT")
            time.sleep(1)
        require(bool(release["source_receipt_id"]), "SOURCE_RECEIPT_MISSING")
        require(
            release["execution_state"] == "NOT_STARTED" and not workspace["snapshot"]["actuals"],
            "ACCEPTANCE_FABRICATED_PRODUCTION",
        )
        require(workspace["freshness"] == "CURRENT", "SYNC_NOT_CURRENT")
        evidence.update(
            final_state="ACTIVE",
            execution_state="NOT_STARTED",
            release_id=release["release_id"],
            source_receipt_id=release["source_receipt_id"],
        )
        evidence["checks"].extend(
            [
                "publication_idempotency",
                "conditional_source_acceptance",
                "source_receipt",
                "acceptance_without_fabricated_production",
            ]
        )
    response_json(client.post("/api/logout", headers=headers))
    response_json(client.get("/api/session"), 401)
    evidence["checks"].append("logout_revokes_session")
    evidence["stage"] = "completed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=local_origin, default="http://127.0.0.1:18080")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Also publish the approved plan to this local simulator",
    )
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120 or not 1 <= args.timeout <= 600:
        parser.error("Solver budget must be 1–120 seconds and wait timeout 1–600 seconds")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    evidence: dict = {
        "status": "STARTED",
        "run_id": run_id,
        "request_id": f"container-solve-{uuid4()}",
        "approval_request_id": f"container-approval-{uuid4()}",
        "publication_request_id": f"container-publication-{uuid4()}",
        "solver_budget_seconds": args.seconds,
        "wait_timeout_seconds": args.timeout,
        "real_model_requests": 0,
    }
    output = ROOT / ".runtime" / f"container-smoke-{run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    started = time.monotonic()
    try:
        with httpx.Client(
            base_url=args.base_url, timeout=15, follow_redirects=False, trust_env=False
        ) as client:
            exercise(
                client,
                seconds=args.seconds,
                timeout=args.timeout,
                execute=args.execute,
                evidence=evidence,
            )
        evidence["status"] = "PASS"
    except Exception as exc:
        evidence["status"] = "FAIL"
        evidence["error"] = str(exc) if isinstance(exc, SmokeFailure) else type(exc).__name__
    finally:
        evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
        output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                key: evidence.get(key)
                for key in (
                    "status",
                    "stage",
                    "native_status",
                    "checker",
                    "operations",
                    "final_state",
                    "error",
                )
            }
        )
    )
    print(f"Evidence: {output}")
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
