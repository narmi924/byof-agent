"""Only registered source endpoints; callers cannot supply arbitrary URLs or SQL."""

import json
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from packages.domain.execution import (
    ActionReceipt,
    PlanSubmission,
    ReplayStart,
    SimulatorCommand,
    TodayRunStart,
)
from packages.domain.models import ConnectorCapabilities, Snapshot


class ConnectorError(Exception):
    def __init__(self, message: str, *, code: str = "SOURCE_UNAVAILABLE", status: int = 503):
        self.code, self.status = code, status
        super().__init__(message)


CONTROL_ERRORS = {
    "ENTERPRISE_TERMS_READ_ONLY": "Enterprise-sourced terms must be updated through the enterprise interface and cannot be overwritten in the disruption simulator.",
    "BUSINESS_TERMS_UNCHANGED": "The delivery rule or quote has not changed.",
    "PRODUCT_NOT_FOUND": "The product does not exist; refresh the current factory.",
    "INVALID_DELIVERY_RULE": "The first delivery quantity must be a whole multiple of the production batch; allowing split delivery requires two deliveries.",
    "RECEIPT_VERSION_CHANGED": "The receipt facts have changed; refresh and record the quote again.",
    "INVALID_EXPEDITE_QUOTE_TIME": "Enter a future whole-minute time; the expedited arrival must be before the original arrival and inside the scheduling window, and the validity must not have passed.",
    "RESOURCE_VERSION_CHANGED": "The machine or worker facts have changed; refresh and confirm again.",
    "INVALID_OVERTIME_WINDOW": "An overtime window must be a complete whole-minute period inside the scheduling window that has not started.",
    "OVERTIME_WINDOW_CONFLICT": "The window overlaps an existing shift, overtime or unavailable period.",
    "OVERTIME_WINDOW_NOT_FOUND": "Only complete overtime windows that have not started can be removed; regular shifts cannot be changed.",
    "BUSINESS_FACTS_CHANGED": "The production facts have changed; compare the business options again with the latest state.",
    "BUSINESS_TERMS_CHANGED": "The delivery rule or quote version has changed; compare again.",
    "ORDER_VERSION_CHANGED": "The order has changed; refresh and confirm again.",
    "ORDER_NOT_FOUND": "The order does not exist; refresh the current factory.",
    "UNSUPPORTED_BATCH_QUANTITY": "The quantity must fit the product batch size; enter zero to cancel the order.",
    "INVALID_DELIVERY_AGREEMENT": "The delivery quantity or date does not meet the current business rules.",
    "PARTIAL_DELIVERY_NOT_ALLOWED": "The current source rule does not allow this split delivery.",
    "QUOTE_NOT_FOUND": "The selected quote does not exist; read the resupply options again.",
    "QUOTE_CHANGED_OR_EXPIRED": "The quote or receipt record has changed; get it again and compare.",
    "QUOTE_PRICE_REQUIRED": "An explicit cost quote is needed before confirming an expedite.",
    "OBJECT_NOT_AVAILABLE": "The machine is not available now; choose an available machine.",
    "RESOURCE_ALREADY_UNAVAILABLE": "The machine already has a stop in this period; adjust the duration or the machine.",
    "WORKER_ALREADY_UNAVAILABLE": "This worker already has leave in this period; adjust the duration.",
    "TREATMENT_FACTS_CHANGED": "Production moved on after the check and the measures were not applied yet; checking again against the latest shop floor.",
    "TREATMENT_CONDITIONS_CHANGED": "The quantity, resource state or time the measures need has changed; the measures were not applied.",
    "INVALID_CONTROL_PAYLOAD": "The control parameters are incomplete or malformed; check the input.",
    "FAILED_INSPECTION_REQUIRED": "Only completed operations confirmed as failed inspection can be scrapped; check the latest quality record.",
    "BATCH_STILL_RUNNING": "This batch still has operations running; finish or stop them before disposing of it.",
    "BATCH_ALREADY_DISPOSED": "This batch has already been disposed of; refresh the shop floor records.",
    "QUALITY_EVIDENCE_REQUIRED": "The original inspection failed; enter the recheck or rework evidence before changing it to passed.",
    "INVALID_REMAINING_WORK": "Enter the confirmed remaining production and changeover minutes.",
    "EXECUTION_NOT_BLOCKED": "This operation is not waiting for a remaining work confirmation; refresh its state.",
    "RECEIPT_NOT_PENDING": "This receipt was already received or cancelled; its expected time can no longer change.",
    "RECEIPT_CANCELLED": "This receipt was cancelled and cannot be received.",
    "OBJECT_NOT_FOUND": "The object does not exist; refresh the factory data.",
    "SOURCE_RUN_CHANGED": "The factory run has changed; refresh and use the current run.",
    "IDEMPOTENCY_CONFLICT": "The action ID was already used for other content; check the result of the original action.",
    "PAUSE_BEFORE_SINGLE_STEP": "Pause the automatic run before advancing step by step.",
    "HORIZON_EXCEEDED": "This advance goes beyond the planning horizon of the current factory.",
    "ORDER_ALREADY_EXISTS_OR_STARTED": "The order ID already exists or its state cannot be added as a new order.",
    "REPLAY_READ_ONLY": "The current run is a replay; it can only advance or pause and cannot change business facts or release plans.",
    "REPLAY_FINISHED": "This replay has stopped; check the replay result.",
    "REPLAY_ORIGIN_MISSING": "The initial record of the original run is missing, so no replay can be created.",
    "REPLAY_INITIAL_STATE_UNSUPPORTED": "The initial record already contains execution history, so it cannot be replayed completely.",
    "TODAY_RUN_INITIAL_STATE_UNSUPPORTED": "The initial record contains execution history, so today's run cannot be created.",
    "TODAY_RUN_ORIGIN_UNAVAILABLE": "The current run has no usable original scenario; refresh the run status.",
    "TODAY_RUN_ORIGIN_MISMATCH": "The original scenario does not match the current run; refresh the run status.",
    "TODAY_RUN_CALENDAR_UNSUPPORTED": "A time in the original scenario falls into a time zone transition gap and cannot be shifted to today safely.",
}


class FactoryHTTP:
    def __init__(self, origin: str, token: str, *, transport=None):
        parsed = urlparse(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ConnectorError("Invalid configured factory origin")
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "factory-sim",
        }:
            raise ConnectorError("Remote factory origins require HTTPS")
        if not token:
            raise ConnectorError("Factory reader token is not configured")
        self.client = httpx.Client(
            base_url=origin,
            headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(10, connect=3),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        missing_ok: bool = False,
    ) -> dict | None:
        try:
            with self.client.stream(method, path, params=params, json=body) as response:
                if missing_ok and response.status_code == 404:
                    return None
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 5_000_000:
                        raise ConnectorError("Factory response exceeds contract size limit")
                if response.status_code != 200:
                    try:
                        error = json.loads(raw)
                        code = error.get("code") if isinstance(error, dict) else None
                    except ValueError:
                        code = None
                    if isinstance(code, str) and code in CONTROL_ERRORS:
                        raise ConnectorError(
                            CONTROL_ERRORS[code],
                            code=code,
                            status=422 if response.status_code == 422 else 409,
                        )
                    raise ConnectorError(f"Factory source returned HTTP {response.status_code}")

                def unique_pairs(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("Duplicate JSON key")
                        result[key] = value
                    return result

                parsed = json.loads(raw, object_pairs_hook=unique_pairs)
                if not isinstance(parsed, dict):
                    raise ValueError("Source response must be an object")
                return parsed
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError("Factory source response could not be read") from exc

    def _get(self, path: str, params: dict | None = None) -> dict:
        result = self._request("GET", path, params=params)
        assert result is not None
        return result

    def action(self, factory_id: str, run_id: str, operation_id: str) -> ActionReceipt | None:
        from urllib.parse import quote

        raw = self._request(
            "GET",
            "/factory/v1/actions/" + quote(operation_id, safe=""),
            params={"factory_id": factory_id, "run_id": run_id},
            missing_ok=True,
        )
        if raw is None:
            return None
        try:
            receipt = ActionReceipt.model_validate(raw)
        except ValidationError as exc:
            raise ConnectorError("Action receipt violates contract") from exc
        if (receipt.factory_id, receipt.run_id, receipt.operation_id) != (
            factory_id,
            run_id,
            operation_id,
        ):
            raise ConnectorError("Action receipt is outside the requested scope")
        return receipt

    def capabilities(self) -> ConnectorCapabilities:
        try:
            return ConnectorCapabilities.model_validate(self._get("/factory/v1/capabilities"))
        except ValidationError as exc:
            raise ConnectorError("Factory capabilities violate contract") from exc

    def snapshot(self, factory_id: str) -> Snapshot:
        try:
            snapshot = Snapshot.model_validate(
                self._get("/factory/v1/snapshot", {"factory_id": factory_id})
            )
        except ValidationError as exc:
            raise ConnectorError("Factory snapshot violates contract") from exc
        if (
            snapshot.factory_id != factory_id
            or not snapshot.source.complete
            or snapshot.source.consistency == "UNVERIFIED"
            or snapshot.source.freshness != "CURRENT"
        ):
            raise ConnectorError(
                "Factory snapshot is incomplete, stale or outside the requested scope"
            )
        return snapshot


class FactoryExecution(FactoryHTTP):
    def submit(self, submission: PlanSubmission) -> ActionReceipt:
        raw = self._request("POST", "/factory/v1/plans", body=submission.model_dump(mode="json"))
        try:
            receipt = ActionReceipt.model_validate(raw)
        except ValidationError as exc:
            raise ConnectorError("Action receipt violates contract") from exc
        if (receipt.factory_id, receipt.run_id, receipt.operation_id, receipt.candidate_hash) != (
            submission.factory_id,
            submission.run_id,
            submission.operation_id,
            submission.candidate.content_hash,
        ):
            raise ConnectorError("Action receipt does not match submitted content")
        return receipt


class FactoryControls(FactoryHTTP):
    def start_today_run(self, factory_id: str, body: TodayRunStart) -> dict:
        from urllib.parse import quote

        raw = self._request(
            "POST",
            "/simulator/v1/factories/" + quote(factory_id, safe="") + "/today-runs",
            body=body.model_dump(mode="json"),
        )
        if (
            raw is None
            or raw.get("factory_id") != factory_id
            or raw.get("request_id") != body.request_id
            or raw.get("origin_run_id") != body.expected_run_id
            or not isinstance(raw.get("run_id"), str)
            or raw["run_id"] == body.expected_run_id
            or not isinstance(raw.get("business_clock"), str)
            or not isinstance(raw.get("horizon_end"), str)
        ):
            raise ConnectorError("Today-run response does not match requested operation")
        return raw

    def start_replay(self, factory_id: str, body: ReplayStart) -> dict:
        from urllib.parse import quote

        raw = self._request(
            "POST",
            "/simulator/v1/factories/" + quote(factory_id, safe="") + "/replays",
            body=body.model_dump(mode="json"),
        )
        if (
            raw is None
            or raw.get("factory_id") != factory_id
            or raw.get("request_id") != body.request_id
            or raw.get("origin_run_id") != body.expected_run_id
            or not isinstance(raw.get("run_id"), str)
            or raw["run_id"] == body.expected_run_id
        ):
            raise ConnectorError("Replay response does not match requested operation")
        return raw

    def command(self, factory_id: str, command: SimulatorCommand) -> dict:
        from urllib.parse import quote

        raw = self._request(
            "POST",
            "/simulator/v1/factories/" + quote(factory_id, safe="") + "/commands",
            body=command.model_dump(mode="json"),
        )
        if (
            raw is None
            or raw.get("factory_id") != factory_id
            or raw.get("run_id") != command.run_id
            or raw.get("request_id") != command.request_id
        ):
            raise ConnectorError("Control response does not match requested operation")
        return raw

    def cancel_treatment(self, factory_id: str, command: SimulatorCommand) -> dict:
        from urllib.parse import quote

        raw = self._request(
            "POST",
            "/simulator/v1/factories/" + quote(factory_id, safe="") + "/commands/cancel",
            body=command.model_dump(mode="json"),
        )
        if raw is None or (raw.get("factory_id"), raw.get("run_id"), raw.get("request_id")) != (
            factory_id,
            command.run_id,
            command.request_id,
        ):
            raise ConnectorError("Cancellation response does not match its approved operation")
        return raw
