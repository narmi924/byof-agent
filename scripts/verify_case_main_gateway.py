"""Budgeted live gateway main case in pre-provisioned byof_probe; retain all business evidence.

Validate without connections: python -m scripts.verify_case_main_gateway --env-file .runtime/live-main.env
Execute once: append --run. The fixed evidence file reserves the budget even after failure.
This script never creates databases, migrates schemas, resets factories, or sends external mail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import uvicorn
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from packages import auth
from packages.agent import case_runtime
from packages.agent.cases import ingest_sources, wake_completed_jobs
from packages.agent.cases_store import CaseRecord, CaseTurn
from packages.agent.decisions import parse_action
from packages.agent.human_tasks import tick_reminders
from packages.domain.models import Snapshot, batch_operations, canonical_hash
from packages.domain.skf import load_skf_snapshot
from packages.integrations.factory_http import FactoryControls, FactoryExecution, FactoryHTTP
from packages.integrations.notification_store import Notification
from packages.integrations.notifications import deliver_notification
from packages.persistence import Membership, User, connect
from packages.planning.publication import Publication, deliver_one
from packages.planning.service import synchronize
from packages.planning.store import SolveJob
from packages.providers.gateway import MAX_OUTPUT_TOKENS
from packages.providers.registry import configured_model
from packages.settings import ROOT, Settings
from scripts.capture_mail import MAILBOX_ROOT, CaptureServer
from scripts.import_factory import import_initial
from services.api.main import create_app as api_app
from services.factory_sim.main import create_app as source_app
from services.factory_sim.service import run_due_tick
from services.factory_sim.storage import World
from services.solver_worker.main import run_once

REPORT_PATH = ROOT / ".runtime" / "p3-main-gateway.json"
ACCOUNTS_PATH = ROOT / ".runtime" / "p3-main-gateway-accounts.json"
MAX_REQUESTS = 40
MAX_SECONDS = 3600
CLOCK_INTERVAL_MS = 5000
REVIEW_MINUTES = 30
EXPECTED_MIGRATION = "0018_solve_action_time"
REQUIRED = {
    "LIVE_MAIN_DATABASE_URL": "BYOF application role of the isolated verification database",
    "LIVE_MAIN_FACTORY_DATABASE_URL": "Simulator source role of the same isolated verification database",
    "LIVE_MAIN_MIGRATION_DATABASE_URL": "Read-only migration check entry of the same isolated verification database",
    "LLM_GATEWAY_URL": "Configured HTTPS gateway",
    "LLM_GATEWAY_API_KEY": "Gateway credential",
    "LLM_MODEL": "Model actually used by BYOF",
}


class ProbeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class APIRejection(ProbeError):
    def __init__(self, status: int, api_code: str):
        self.status, self.api_code = status, api_code
        super().__init__("BUSINESS_API_REJECTED")


class ProbeSettings(Settings):
    live_main_database_url: SecretStr = SecretStr("")
    live_main_factory_database_url: SecretStr = SecretStr("")
    live_main_migration_database_url: SecretStr = SecretStr("")


def local_settings(**values: Any) -> Settings:
    return Settings(**{"_env_file": None, **values})


def validate_configuration(settings: ProbeSettings) -> list[dict[str, str]]:
    missing = [
        {"name": name, "purpose": purpose}
        for name, purpose in REQUIRED.items()
        if not (
            value.get_secret_value()
            if isinstance(value := getattr(settings, name.lower()), SecretStr)
            else value
        )
    ]
    if missing:
        return missing
    targets = []
    for field, role in (
        ("live_main_database_url", "byof_app"),
        ("live_main_factory_database_url", "factory_sim_app"),
        ("live_main_migration_database_url", "byof_owner"),
    ):
        try:
            url = make_url(getattr(settings, field).get_secret_value())
        except Exception:
            raise ProbeError("INVALID_PROBE_DATABASE_URL") from None
        if (
            url.drivername != "postgresql+psycopg"
            or url.database != "byof_probe"
            or url.username != role
            or url.host not in {"localhost", "127.0.0.1"}
            or not url.password
            or url.query
        ):
            raise ProbeError("PROBE_DATABASE_SCOPE_REQUIRED")
        targets.append((url.host, url.port or 5432, url.database))
    if len(set(targets)) != 1:
        raise ProbeError("PROBE_DATABASE_TARGET_MISMATCH")
    if settings.llm_provider != "gateway":
        raise ProbeError("GATEWAY_REQUIRED")
    configured_model(settings)  # Adapter construction validates the URL; no request is made.
    return []


def code_hashes() -> dict[str, str]:
    files = [ROOT / "uv.lock", Path(__file__).resolve()]
    for folder in ("packages", "services"):
        files.extend((ROOT / folder).rglob("*.py"))
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(set(files))
    }


class Evidence:
    def __init__(self, path: Path, model: str, *, max_requests: int = MAX_REQUESTS):
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_REQUESTS:
            raise ProbeError("INVALID_LIVE_REQUEST_BUDGET")
        self.path = path
        self.started = time.monotonic()
        self.data: dict[str, Any] = {
            "status": "reserved",
            "created_at": datetime.now(UTC).isoformat(),
            "database": "byof_probe",
            "model": model,
            "max_requests": max_requests,
            "requests_started": 0,
            "requests_per_turn": 4,
            "timeout_seconds": 30,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "retries": 0,
            "actual_token_usage": None,
            "clock_interval_ms": CLOCK_INTERVAL_MS,
            "requested_review_business_minutes": REVIEW_MINUTES,
            "max_seconds": MAX_SECONDS,
            "real_emails": 0,
            "mail_channel": "LOCAL_CAPTURE",
            "code_hashes": code_hashes(),
            "stages": [],
            "model_decisions": [],
            "operations": [],
            "control_requests": [],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            json.dump(self.data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())

    def save(self):
        self.data["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        temporary = self.path.with_suffix(".pending")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(self.data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.path)

    def stage(self, name: str, snapshot: Snapshot):
        self.data["stages"].append(
            {
                "name": name,
                "snapshot_hash": snapshot.content_hash,
                "business_clock": snapshot.snapshot_clock.isoformat(),
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        )
        self.save()
        print(
            json.dumps(
                {
                    "stage": name,
                    "business_clock": snapshot.snapshot_clock.isoformat(),
                    "requests_started": self.data["requests_started"],
                }
            ),
            flush=True,
        )

    def case(self, detail: dict):
        self.data["case_state"], self.data["case_error"] = detail["state"], detail["error_code"]
        self.data["operations"] = [
            {
                "operation_id": row["operation_id"],
                "action": row["action"],
                "snapshot_id": row["snapshot_id"],
                "status": (row["result"] or {}).get("status"),
                "code": (row["result"] or {}).get("code"),
                **{
                    key: row["result"][key]
                    for key in ("job_id", "task_id", "candidate_id")
                    if row["result"] and key in row["result"]
                },
            }
            for row in detail["operations"]
        ]
        self.save()


class BudgetedModel:
    def __init__(self, model, evidence: Evidence, *, forbidden: tuple[str, ...] = ()):
        self.model, self.evidence, self.forbidden = model, evidence, forbidden
        self.case_id: str | None = None

    @property
    def max_case_requests(self) -> int:
        return self.evidence.data["max_requests"]

    def complete(self, prompt: str) -> str:
        context = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
        if self.case_id is None or context["case"]["case_id"] != self.case_id:
            raise ProbeError("MODEL_CASE_SCOPE_MISMATCH")
        if any(value and value in prompt for value in self.forbidden) or any(
            '"' + key + '"' in prompt
            for key in ("future_events", "replay_state", "control_token", "expected_actions")
        ):
            raise ProbeError("MODEL_CONTEXT_CONTAINS_PRIVATE_CONTROL")
        if self.evidence.data["requests_started"] >= self.evidence.data["max_requests"]:
            raise ProbeError("LIVE_BUDGET_EXHAUSTED")
        if time.monotonic() - self.evidence.started >= MAX_SECONDS:
            raise ProbeError("SCENARIO_TIME_LIMIT")
        self.evidence.data["requests_started"] += 1
        recorded: dict[str, Any] = {
            "request": self.evidence.data["requests_started"],
            "state": "STARTED",
        }
        self.evidence.data["model_decisions"].append(recorded)
        self.evidence.save()  # Reserve before the network; a crash consumes this attempt.
        started = time.monotonic()
        try:
            result = self.model.complete(prompt)
            try:
                decision = parse_action(result)
                recorded.update(
                    state="RETURNED",
                    action=decision.action,
                    parameters_hash=canonical_hash(decision.parameters.model_dump(mode="json")),
                )
            except ValueError:
                recorded.update(state="RETURNED", action="INVALID_MODEL_ACTION")
        except BaseException as exc:
            recorded.update(state="FAILED", error_code=type(exc).__name__)
            raise
        finally:
            recorded["seconds"] = round(time.monotonic() - started, 3)
            self.evidence.save()
        return result


def synthetic_input(factory_id: str) -> Snapshot:
    original = load_skf_snapshot(development=True)
    data = original.model_dump(mode="python", exclude={"content_hash"})
    data["factory_id"] = data["profile"]["factory_id"] = factory_id
    data["orders"][0]["quantity"] *= 3
    data["profile"].update(version="gateway-main-synthetic/1", evidence_mode="synthetic")
    data["profile"]["policy"].update(
        policy_version="gateway-main-synthetic/1", progress_revalidation_enabled=True
    )
    result = Snapshot.model_validate(data)
    if len(batch_operations(result)[1]) != 24 or result.horizon != original.horizon:
        raise ProbeError("SCENARIO_INPUT_INVALID")
    return result


@contextmanager
def serve(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not server.started:
            raise ProbeError("LOCAL_HTTP_START_FAILED")
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=12)
        listener.close()
        if thread.is_alive():
            raise ProbeError("LOCAL_HTTP_STOP_FAILED")


class Scenario:
    def __init__(self, settings: ProbeSettings, evidence: Evidence):
        self.settings, self.evidence = settings, evidence
        self.stack = ExitStack()
        self.factory = "gateway-main-" + uuid4().hex
        self.case_id = ""
        self.stopping = threading.Event()
        self.clock_errors: list[str] = []
        self.ticks = 0
        self.clients: dict[str, httpx.Client] = {}
        self.accounts: dict[str, dict[str, str]] = {}

    def __enter__(self):
        try:
            return self.open()
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *_):
        self.stack.close()

    def open(self):
        self.engine = connect(self.settings.live_main_database_url.get_secret_value())
        self.source_engine = connect(
            self.settings.live_main_factory_database_url.get_secret_value()
        )
        owner = connect(self.settings.live_main_migration_database_url.get_secret_value())
        for engine in (self.engine, self.source_engine, owner):
            self.stack.callback(engine.dispose)
        for engine, role, forbidden_schema in (
            (self.engine, "byof_app", "factory_sim"),
            (self.source_engine, "factory_sim_app", "byof"),
        ):
            with engine.connect() as connection:
                actual = connection.execute(text("SELECT current_database(), current_user")).one()
                if tuple(actual) != ("byof_probe", role) or connection.scalar(
                    text("SELECT has_schema_privilege(current_user, :schema, 'USAGE')"),
                    {"schema": forbidden_schema},
                ):
                    raise ProbeError("DATABASE_ROLE_ISOLATION_FAILED")
        with owner.connect() as connection:
            if tuple(connection.execute(text("SELECT current_database(), current_user")).one()) != (
                "byof_probe",
                "byof_owner",
            ):
                raise ProbeError("DATABASE_OWNER_SCOPE_FAILED")
            self.evidence.data["migration_head"] = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            if self.evidence.data["migration_head"] != EXPECTED_MIGRATION:
                raise ProbeError("PROBE_MIGRATION_HEAD_MISMATCH")
        with Session(self.engine) as db:
            if db.scalar(
                select(CaseRecord.case_id)
                .where(CaseRecord.state.not_in(("RESOLVED", "HANDED_OFF", "CANCELLED")))
                .limit(1)
            ) or db.scalar(
                select(SolveJob.job_id).where(SolveJob.state.in_(("QUEUED", "RUNNING"))).limit(1)
            ):
                raise ProbeError("PROBE_QUEUES_NOT_IDLE")
            if db.scalar(
                select(Publication.release_id)
                .where(Publication.state.in_(("QUEUED", "DELIVERING", "UNKNOWN")))
                .limit(1)
            ) or db.scalar(
                select(Notification.notification_id)
                .where(
                    Notification.send_state.in_(
                        ("QUEUED", "CLAIMED", "SENDING", "NOT_CONFIGURED", "NOT_ENABLED", "UNKNOWN")
                    )
                )
                .limit(1)
            ):
                raise ProbeError("PROBE_EXTERNAL_ACTIONS_NOT_IDLE")
        with Session(self.source_engine) as db:
            if db.scalar(select(World.factory_id).where(World.mode == "RUNNING").limit(1)):
                raise ProbeError("PROBE_FACTORY_ALREADY_RUNNING")
        initial = import_initial(self.source_engine, synthetic_input(self.factory))
        self.initial = initial
        self.evidence.data.update(
            factory_id=self.factory, run_id=initial.run_id, input_hash=initial.content_hash
        )
        self.evidence.save()
        self.tokens = {
            role: secrets.token_urlsafe(32) for role in ("reader", "writer", "controller")
        }
        source_settings = local_settings(
            environment="test",
            factory_database_url=self.settings.live_main_factory_database_url,
            factory_api_token=SecretStr(self.tokens["reader"]),
            factory_execution_token=SecretStr(self.tokens["writer"]),
            factory_control_token=SecretStr(self.tokens["controller"]),
        )
        source_origin = self.stack.enter_context(serve(source_app(source_settings)))
        self.reader = FactoryHTTP(source_origin, self.tokens["reader"])
        self.writer = FactoryExecution(source_origin, self.tokens["writer"])
        self.controls = FactoryControls(source_origin, self.tokens["controller"])
        for factory_client in (self.reader, self.writer, self.controls):
            self.stack.callback(factory_client.close)
        self.mailbox = mailbox = MAILBOX_ROOT / self.factory
        mailbox.mkdir(parents=True, exist_ok=False)
        capture = CaptureServer(("127.0.0.1", 0), mailbox=mailbox)
        mail_thread = threading.Thread(target=capture.serve_forever, daemon=True)
        mail_thread.start()

        def stop_mail():
            capture.shutdown()
            capture.server_close()
            mail_thread.join(timeout=3)
            if mail_thread.is_alive():
                raise ProbeError("MAIL_CAPTURE_STOP_FAILED")

        self.stack.callback(stop_mail)
        self.api_settings = local_settings(
            environment="test",
            database_url=self.settings.live_main_database_url,
            factory_api_url=source_origin,
            factory_api_token=SecretStr(self.tokens["reader"]),
            factory_execution_token=SecretStr(self.tokens["writer"]),
            factory_control_token=SecretStr(self.tokens["controller"]),
            smtp_mode="capture",
            smtp_host="127.0.0.1",
            smtp_port=capture.server_address[1],
            smtp_from="byof@capture.invalid",
            smtp_username="",
            smtp_password="",
            allow_real_email=False,
            test_email_recipient="",
        )
        self.api_origin = self.stack.enter_context(serve(api_app(self.api_settings)))
        for role in ("planner", "maintainer", "manager", "admin"):
            self.accounts[role] = {
                "username": role + "-" + uuid4().hex,
                "password": secrets.token_urlsafe(32),
            }
        with ACCOUNTS_PATH.open("x", encoding="utf-8") as output:
            json.dump(
                {
                    "factory_id": self.factory,
                    "api_origin": self.api_origin,
                    "accounts": self.accounts,
                },
                output,
                indent=2,
            )
        with Session(self.engine) as db, db.begin():
            for role, account in self.accounts.items():
                db.add(
                    User(
                        user_id=account["username"],
                        username=account["username"],
                        password_hash=auth.hasher.hash(account["password"]),
                        active=True,
                    )
                )
                db.flush()
                db.add(Membership(user_id=account["username"], factory_id=self.factory, role=role))
        for role, account in self.accounts.items():
            client = self.stack.enter_context(httpx.Client(base_url=self.api_origin, timeout=15))
            headers = {"Origin": self.api_settings.public_origin}
            response = client.post("/api/login", json=account, headers=headers)
            if response.status_code != 200:
                raise ProbeError("ROLE_LOGIN_FAILED")
            headers["X-CSRF-Token"] = response.json()["csrf_token"]
            client.headers.update(headers)
            self.clients[role] = client
        self.post("/sync", {})
        for role in ("planner", "maintainer", "manager"):
            self.request(
                "POST",
                f"/api/admin/factories/{self.factory}/notification-contacts",
                {
                    "request_id": str(uuid4()),
                    "role": role,
                    "user_id": self.accounts[role]["username"],
                    "email": role + "@capture.invalid",
                    "enabled": True,
                    "expected_version": 0,
                },
                role="admin",
            )
        self.evidence.data.update(
            mailbox_path=str(mailbox.relative_to(ROOT)),
            source_origin=source_origin,
            api_origin=self.api_origin,
        )
        self.evidence.save()
        return self

    def request(self, method: str, path: str, body=None, *, role="planner"):
        result = self.clients[role].request(method, path, json=body)
        if result.status_code != 200:
            try:
                code = result.json().get("code", "API_REJECTED")
            except ValueError:
                code = "API_REJECTED"
            self.evidence.data["last_api_failure"] = {"status": result.status_code, "code": code}
            self.evidence.save()
            raise APIRejection(result.status_code, code)
        return result.json()

    def read(self, path: str):
        return self.request("GET", f"/api/factories/{self.factory}" + path)

    def post(self, path: str, body: dict, *, role="planner"):
        # Persist action identity before sending. Unknown results never generate a replacement ID.
        self.evidence.data["last_operator_request"] = {
            "path": path,
            "request_id": body.get("request_id"),
            "payload_hash": canonical_hash(body),
        }
        self.evidence.save()
        return self.request("POST", f"/api/factories/{self.factory}" + path, body, role=role)

    def control(self, kind: str, payload: dict):
        from packages.domain.execution import SimulatorCommand

        command = SimulatorCommand.model_validate(
            {
                "request_id": str(uuid4()),
                "run_id": self.initial.run_id,
                "kind": kind,
                "payload": payload,
            }
        )
        self.evidence.data["last_control_request"] = {
            "request_id": command.request_id,
            "kind": kind,
            "payload_hash": canonical_hash(payload),
        }
        self.evidence.data["control_requests"].append(self.evidence.data["last_control_request"])
        self.evidence.save()
        return self.controls.command(self.factory, command)

    def snapshot(self):
        if self.clock_errors:
            raise ProbeError("CLOCK_WORKER_FAILED")
        if time.monotonic() - self.evidence.started > MAX_SECONDS:
            raise ProbeError("SCENARIO_TIME_LIMIT")
        return self.reader.snapshot(self.factory)

    def start_clock(self):
        self.control("clock.run", {"interval_ms": CLOCK_INTERVAL_MS})

        def clock_loop():
            try:
                while not self.stopping.is_set():
                    self.ticks += int(run_due_tick(self.source_engine))
                    self.stopping.wait(0.03)
            except Exception as exc:
                self.clock_errors.append(type(exc).__name__)

        self.clock_thread = threading.Thread(target=clock_loop, daemon=True)
        self.clock_thread.start()

        def stop_clock():
            self.stopping.set()
            self.clock_thread.join(timeout=15)
            self.evidence.data["clock_ticks"] = self.ticks
            self.evidence.data["clock_worker_stopped"] = not self.clock_thread.is_alive()
            self.evidence.data["clock_pause_commands"] = sum(
                request["kind"] == "clock.pause"
                for request in self.evidence.data["control_requests"]
            )
            self.evidence.data["captured_mail_count"] = len(list(self.mailbox.glob("*.eml")))
            self.evidence.save()
            if self.clock_thread.is_alive():
                raise ProbeError("CLOCK_WORKER_STOP_FAILED")

        self.stack.callback(stop_clock)

    def message(self, value: str):
        self.post(f"/cases/{self.case_id}/messages", {"request_id": str(uuid4()), "message": value})

    def pump(self, model: BudgetedModel):
        synchronize(self.engine, self.reader, self.factory)
        ingest_sources(self.engine)
        run_once(self.engine)
        wake_completed_jobs(self.engine)
        tick_reminders(self.engine)
        deliver_notification(self.engine, self.api_settings)
        case_runtime.process_case(self.engine, self.reader, model)
        detail = self.read(f"/cases/{self.case_id}")
        self.evidence.case(detail)
        if detail["error_code"] == "MODEL_BUDGET_EXHAUSTED":
            raise ProbeError("LIVE_BUDGET_EXHAUSTED")
        if detail["error_code"] or detail["state"] in {"HANDED_OFF", "CANCELLED"}:
            raise ProbeError("CASE_DID_NOT_CONTINUE")
        return detail

    def publish(self, candidate: dict, *, initial=False):
        identity, digest = candidate["candidate_id"], candidate["content_hash"]
        current = self.snapshot()
        # Progress review requires an observed source revision after the original snapshot.
        while not initial and current.content_hash == candidate["binding"]["snapshot_hash"]:
            time.sleep(0.25)
            current = self.snapshot()
        seconds_required = minimum_execution_seconds(current, candidate)
        self.evidence.data.setdefault("execution_estimates", []).append(
            {
                "candidate_id": identity,
                "candidate_hash": digest,
                "business_clock": current.snapshot_clock.isoformat(),
                "minimum_remaining_seconds": seconds_required,
            }
        )
        self.evidence.save()
        if seconds_required > MAX_SECONDS - (time.monotonic() - self.evidence.started):
            raise ProbeError("PLAN_EXCEEDS_PROBE_TIME_BUDGET")
        endpoint = "approvals" if initial else "progress-approvals"
        for scope in ("publish_plan", *candidate["required_consents"]):
            self.post(
                f"/candidates/{identity}/{endpoint}",
                {
                    "request_id": str(uuid4()),
                    "candidate_hash": digest,
                    "action_scope": scope,
                    "decision": "APPROVED",
                },
                role="planner" if scope == "publish_plan" else "manager",
            )
        for attempt in range(1 if initial else 3):
            body = {"request_id": str(uuid4()), "candidate_hash": digest}
            if not initial:
                certificate = self.post(
                    f"/candidates/{identity}/validations",
                    {"request_id": str(uuid4()), "candidate_hash": digest},
                )
                body["certificate_id"] = certificate["certificate_id"]
            try:
                release = self.post(f"/candidates/{identity}/publications", body)
            except APIRejection as exc:
                if (
                    not initial
                    and exc.status == 409
                    and exc.api_code == "VALIDATION_BINDING_CHANGED"
                ):
                    self.evidence.data.setdefault("publication_recovery", []).append(
                        {
                            "attempt": attempt + 1,
                            "request_id": body["request_id"],
                            "error_code": exc.api_code,
                        }
                    )
                    self.evidence.save()
                    continue
                raise
            if not deliver_one(self.engine, self.reader, self.writer):
                raise ProbeError("PUBLICATION_NOT_CLAIMED")
            releases = self.read("/workspace")["publications"]
            record = next(
                r for r in releases if r["release"]["release_id"] == release["release_id"]
            )
            result = record["release"]
            self.evidence.data.setdefault("publication_attempts", []).append(record)
            self.evidence.save()
            if result["source_state"] == "ACTIVE" and result["source_receipt_id"]:
                break
            if (
                initial
                or result["source_state"] != "REJECTED"
                or not result["source_receipt_id"]
                or record["error_code"] != "SOURCE_CONDITIONS_CHANGED"
            ):
                raise ProbeError("SOURCE_ACCEPTANCE_NOT_CONFIRMED")
        else:
            raise ProbeError("PUBLICATION_RECOVERY_BUDGET_EXHAUSTED")
        if self.snapshot().active_plan_hash != digest:
            raise ProbeError("ACTIVE_PLAN_NOT_MATCHED")
        self.evidence.data.setdefault("releases", []).append(result)
        self.evidence.save()
        return result


def physical_minutes(actual) -> float:
    return sum((s.end_at - s.start_at).total_seconds() / 60 for s in actual.segments)


def minimum_execution_seconds(current: Snapshot, candidate: dict) -> float:
    """Forecast the minimum wall time without changing the accepted plan or source speed."""
    finish_at = max(datetime.fromisoformat(a["end_at"]) for a in candidate["assignments"])
    minutes = max(0.0, (finish_at - current.snapshot_clock).total_seconds() / 60)
    return minutes * CLOCK_INTERVAL_MS / 1000


def compared_candidates(detail: dict) -> set[tuple[str, str]]:
    return {
        (row["candidate_id"], row["candidate_hash"])
        for operation in detail["operations"]
        if operation["action"] == "compare_candidates"
        and (operation["result"] or {}).get("status") == "OK"
        for row in operation["result"]["candidates"]
        if row["current"] and (row["current_checker"] or {}).get("status") == "PASS"
    }


def verify_completed_factory(initial: Snapshot, current: Snapshot):
    if (
        len(current.actuals) != 32
        or any(a.state != "COMPLETED" or a.quality_state != "PASSED" for a in current.actuals)
        or any(order.status != "COMPLETED" for order in current.orders)
    ):
        raise ProbeError("CASE_CLOSED_WITHOUT_ACTUAL_COMPLETION")
    consumed_ids = [c.event_id for a in current.actuals for c in a.consumed]
    if len(consumed_ids) != len(set(consumed_ids)):
        raise ProbeError("DUPLICATE_MATERIAL_CONSUMPTION")
    received_before = {r.receipt_id for r in initial.receipts if r.status == "RECEIVED"}
    for original in initial.inventory:
        stock = next(i for i in current.inventory if i.material_id == original.material_id)
        receipts = sum(
            r.quantity
            for r in current.receipts
            if r.material_id == stock.material_id
            and r.status == "RECEIVED"
            and r.receipt_id not in received_before
        )
        consumed = sum(
            c.quantity
            for a in current.actuals
            for c in a.consumed
            if c.material_id == stock.material_id
        )
        reserved = sum(
            r.quantity for r in current.reservations if r.material_id == stock.material_id
        )
        if (
            stock.on_hand != original.on_hand + receipts - consumed
            or stock.reserved != original.reserved + reserved
            or stock.on_hand < stock.reserved
        ):
            raise ProbeError("MATERIAL_LEDGER_MISMATCH")


def run_main_flow(ctx: Scenario, model: BudgetedModel):
    job = ctx.post("/solve", {"request_id": str(uuid4()), "allow_overtime": False, "time_limit": 2})
    if not run_once(ctx.engine):
        raise ProbeError("INITIAL_SOLVER_NOT_CLAIMED")
    workspace = ctx.read("/workspace")
    saved_job = next(r for r in workspace["jobs"] if r["job_id"] == job["job_id"])
    baseline = next(
        r["candidate"]
        for r in workspace["candidates"]
        if r["candidate"]["candidate_id"] == saved_job["candidate_id"]
    )
    if not baseline["has_solution"] or baseline["checker"]["status"] != "PASS":
        raise ProbeError("INITIAL_PLAN_NOT_CHECKED")
    ctx.publish(baseline, initial=True)
    created = ctx.post(
        "/cases",
        {
            "request_id": str(uuid4()),
            "start_new": True,
            "message": "Follow the current production. Query the actual state first and wait for new events when it is normal; an initial plan exists, so no repeated trial is needed. On a disruption, check the facts and ask the responsible owner for missing information, and keep following while waiting.",
        },
    )
    ctx.case_id = model.case_id = created["case_id"]
    ctx.evidence.data["case_id"] = ctx.case_id
    ctx.start_clock()
    normal_checked = False
    interrupted = independent = None
    blocked = None
    requested_at = None
    task = None
    urgent_added = preference_confirmed = replied = restored = False
    reply_clock = None
    accepted = None
    compared: set[tuple[str, str]] = set()
    recovery_after = max(
        datetime.fromisoformat(a["start_at"]) for a in baseline["assignments"]
    ) + timedelta(minutes=1)
    while True:
        current = ctx.snapshot()
        if not ctx.clock_thread.is_alive():
            raise ProbeError("CLOCK_NOT_RUNNING")
        if not normal_checked and current.actuals:
            detail = ctx.pump(model)
            if detail["state"] == "RESOLVED":
                raise ProbeError("CASE_CLOSED_BEFORE_SCENARIO")
            normal_checked = True
            ctx.evidence.stage("normal_production", current)
        if (
            normal_checked
            and interrupted is None
            and current.snapshot_clock >= ctx.initial.snapshot_clock + timedelta(minutes=13)
        ):
            for active in current.actuals:
                others = [
                    a
                    for a in current.actuals
                    if a.state == "IN_PROGRESS"
                    and a.resource_id != active.resource_id
                    and a.worker_id != active.worker_id
                ]
                if active.state == "IN_PROGRESS" and active.consumed and others:
                    interrupted, independent = active, others[0]
                    break
            if interrupted is not None:
                ctx.control("resource.down", {"resource_id": interrupted.resource_id})
                current = ctx.snapshot()
                blocked = next(
                    a for a in current.actuals if a.operation_id == interrupted.operation_id
                )
                if (
                    blocked.remaining_minutes is not None
                    or blocked.consumed != interrupted.consumed
                ):
                    raise ProbeError("FAULT_HISTORY_INVALID")
                ctx.evidence.stage("unknown_outage", current)
        if interrupted is not None:
            detail = ctx.pump(model)
            tasks = ctx.read("/human-tasks")["tasks"]
            if task is None:
                task = next(
                    (
                        t
                        for t in tasks
                        if t["task_type"] == "INFORMATION"
                        and t["state"] == "OPEN"
                        and t["owner_role"] == "maintainer"
                        and t["subject_id"] in {interrupted.operation_id, interrupted.resource_id}
                    ),
                    None,
                )
                if task:
                    requested_at = ctx.snapshot().snapshot_clock
                    ctx.evidence.stage("information_requested", ctx.snapshot())
            current = ctx.snapshot()
            if (
                task
                and not urgent_added
                and requested_at is not None
                and current.snapshot_clock >= requested_at + timedelta(minutes=3)
            ):
                assert independent is not None and blocked is not None
                other = next(
                    a for a in current.actuals if a.operation_id == independent.operation_id
                )
                stalled = next(
                    a for a in current.actuals if a.operation_id == interrupted.operation_id
                )
                if (
                    physical_minutes(other) <= physical_minutes(independent)
                    or stalled.consumed != blocked.consumed
                    or stalled.remaining_minutes is not None
                ):
                    raise ProbeError("WAITING_PRODUCTION_EVIDENCE_FAILED")
                ctx.evidence.stage("independent_production_during_wait", current)
                urgent = ctx.initial.orders[0].model_dump(mode="json")
                urgent.update(
                    order_id="urgent-" + uuid4().hex,
                    quantity=50,
                    priority_weight=5,
                    due_at=(ctx.initial.snapshot_clock + timedelta(hours=8)).isoformat(),
                )
                ctx.control("order.add", urgent)
                ctx.message(
                    "A rush order came in. Propose a stability-first preference for this case; I will confirm a tardiness bound of 1000 and an added overtime bound of 0. "
                    "Solve after the repair data is verified and the execution source recovers, with regular hours and a solve budget of at most 10 seconds; "
                    f"reserve at least {REVIEW_MINUTES} business minutes for review before new actions; compare the actual plans and ask the planning owner for approval."
                )
                urgent_added = True
                ctx.evidence.stage("urgent_order_entered", ctx.snapshot())
            if urgent_added and not preference_confirmed:
                preferences = ctx.read("/preferences")
                proposed = next(
                    (
                        p
                        for p in preferences["agent_proposals"]
                        if p["case_id"] == ctx.case_id and p["selection"] == "stability_first"
                    ),
                    None,
                )
                if proposed:
                    proposal = ctx.post(
                        "/preferences/proposals",
                        {
                            "request_id": str(uuid4()),
                            "scope_type": "CASE",
                            "scope_id": ctx.case_id,
                            "definition": {
                                "selection": "stability_first",
                                "max_weighted_tardiness": 1000,
                                "max_incremental_overtime_minutes": 0,
                            },
                            "expected_version": 0,
                            "reason": "Explicitly confirmed after checking the stability, tardiness and added overtime bounds.",
                            "source_proposal_id": proposed["proposal_id"],
                        },
                    )
                    ctx.post(
                        f"/preferences/proposals/{proposal['proposal_id']}/confirmations",
                        {
                            "request_id": str(uuid4()),
                            "expected_state_version": preferences["state_version"],
                        },
                    )
                    preference_confirmed = True
                    ctx.evidence.stage("planner_preference_confirmed", ctx.snapshot())
            if preference_confirmed and not replied:
                assert task is not None
                task = ctx.read(f"/human-tasks/{task['task_id']}")
                possible = {
                    "repair_eta": (current.snapshot_clock + timedelta(minutes=10)).isoformat(),
                    "remaining_minutes": interrupted.remaining_minutes,
                    "remaining_setup_minutes": interrupted.remaining_setup_minutes,
                    "comment": "Maintenance checked the last work record before the stop; this is a named reply and the execution source still has to confirm separately.",
                }
                if set(task["fields"]) - set(possible):
                    raise ProbeError("REQUESTED_INFORMATION_UNAVAILABLE")
                response = ctx.post(
                    f"/human-tasks/{task['task_id']}/responses",
                    {
                        "request_id": str(uuid4()),
                        "expected_task_version": task["version"],
                        "answer": {f: possible[f] for f in task["fields"]},
                    },
                    role="maintainer",
                )
                unchanged = next(
                    a for a in ctx.snapshot().actuals if a.operation_id == interrupted.operation_id
                )
                if response["state"] != "RESPONDED" or unchanged.remaining_minutes is not None:
                    raise ProbeError("HUMAN_REPLY_CHANGED_SOURCE")
                replied, reply_clock = True, ctx.snapshot().snapshot_clock
                ctx.evidence.stage("named_reply_without_source_mutation", ctx.snapshot())
            if (
                replied
                and not restored
                and reply_clock is not None
                and current.snapshot_clock
                >= max(recovery_after, reply_clock + timedelta(minutes=2))
            ):
                ctx.control(
                    "execution.confirm_remaining",
                    {
                        "operation_id": interrupted.operation_id,
                        "remaining_minutes": interrupted.remaining_minutes,
                        "remaining_setup_minutes": interrupted.remaining_setup_minutes,
                    },
                )
                ctx.control("resource.restore", {"resource_id": interrupted.resource_id})
                restored = True
                ctx.evidence.stage("source_confirmed_recovery", ctx.snapshot())
            compared.update(compared_candidates(detail))
            if restored and compared and accepted is None:
                review = next(
                    (t for t in tasks if t["task_type"] == "APPROVAL" and t["state"] == "OPEN"),
                    None,
                )
                if review:
                    workspace = ctx.read("/workspace")
                    plan = next(
                        r["candidate"]
                        for r in workspace["candidates"]
                        if r["candidate"]["candidate_id"] == review["review"]["candidate_id"]
                    )
                    if (plan["candidate_id"], plan["content_hash"]) not in compared:
                        raise ProbeError("REVIEW_CANDIDATE_NOT_COMPARED")
                    requested_start = plan.get("new_actions_not_before")
                    if not requested_start or datetime.fromisoformat(requested_start) < (
                        datetime.fromisoformat(plan["effective_not_before"])
                        + timedelta(minutes=REVIEW_MINUTES)
                    ):
                        raise ProbeError("REQUESTED_REVIEW_TIME_NOT_RESERVED")
                    accepted = ctx.publish(plan)
                    ctx.evidence.stage("human_approved_and_source_accepted", ctx.snapshot())
            if detail["state"] == "RESOLVED":
                if accepted is None:
                    raise ProbeError("CASE_CLOSED_WITHOUT_NEW_PLAN_EXECUTION")
                current = ctx.snapshot()
                verify_completed_factory(ctx.initial, current)
                closing = detail["closure"]["closing_evidence"]
                if (
                    closing["release_id"] != accepted["release_id"]
                    or closing["source_receipt_id"] != accepted["source_receipt_id"]
                    or closing["open_human_tasks"]
                    or not closing["risk_summary"]
                    or len(closing["verified_scope_operation_ids"]) != 32
                    or set(closing["completed_operation_ids"])
                    != set(closing["verified_scope_operation_ids"])
                ):
                    raise ProbeError("CLOSURE_EVIDENCE_INCOMPLETE")
                ctx.evidence.data["closing_evidence"] = closing
                with Session(ctx.engine) as db:
                    turns = list(
                        db.scalars(select(CaseTurn).where(CaseTurn.case_id == ctx.case_id))
                    )
                    if (
                        any(t.model_requests > 4 for t in turns)
                        or sum(t.model_requests for t in turns)
                        != ctx.evidence.data["requests_started"]
                    ):
                        raise ProbeError("MODEL_BUDGET_LEDGER_MISMATCH")
                    if (
                        db.scalar(
                            select(func.count())
                            .select_from(CaseRecord)
                            .where(CaseRecord.factory_id == ctx.factory)
                        )
                        != 1
                    ):
                        raise ProbeError("CASE_CORRELATION_FAILED")
                ctx.evidence.stage("execution_verified_and_case_resolved", current)
                return
        time.sleep(0.5)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--max-requests",
        type=int,
        default=MAX_REQUESTS,
        choices=range(1, MAX_REQUESTS + 1),
        metavar="1..40",
        help="Lower the total live-call cap, including contract corrections (default: 40).",
    )
    args = parser.parse_args(argv)
    evidence = None
    try:
        files = [ROOT / ".env"]
        if args.env_file:
            path = args.env_file.resolve()
            if not path.is_relative_to(ROOT) or not path.is_file():
                raise ProbeError("PROJECT_ENV_FILE_REQUIRED")
            files.append(path)
        setting_values: dict[str, Any] = {"_env_file": files}
        settings = ProbeSettings(**setting_values)
        missing = validate_configuration(settings)
        if missing:
            print(
                json.dumps(
                    {"status": "configuration_missing", "missing": missing}, ensure_ascii=False
                )
            )
            return 2
        if not args.run:
            print(
                json.dumps(
                    {
                        "status": "configuration_validated",
                        "database": "byof_probe",
                        "network_requests": 0,
                        "run_flag_required": True,
                        "max_requests": args.max_requests,
                    }
                )
            )
            return 0
        if ACCOUNTS_PATH.exists():
            raise ProbeError("PRIOR_PROBE_ACCOUNTS_EXIST")
        evidence = Evidence(REPORT_PATH, settings.llm_model, max_requests=args.max_requests)
        with Scenario(settings, evidence) as ctx:
            forbidden = (
                *ctx.tokens.values(),
                settings.llm_gateway_api_key.get_secret_value(),
                *(a["password"] for a in ctx.accounts.values()),
            )
            model = BudgetedModel(configured_model(settings), evidence, forbidden=forbidden)
            run_main_flow(ctx, model)
        evidence.data["status"] = "passed"
    except BaseException as exc:
        code = exc.code if isinstance(exc, ProbeError) else type(exc).__name__
        if evidence is None:
            print(json.dumps({"status": "not_started", "error_code": code, "network_requests": 0}))
            return 2
        evidence.data.update(status="failed", error_code=code)
    finally:
        if evidence:
            evidence.save()
    print(
        json.dumps(
            {
                "status": evidence.data["status"],
                "requests_started": evidence.data["requests_started"],
                "evidence_path": str(REPORT_PATH),
                "real_emails": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0 if evidence.data["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
