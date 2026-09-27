"""Live-probe publication recovery uses known outcomes, preserves approvals, and stays bounded."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from packages.domain.models import canonical_hash
from scripts import verify_case_main_gateway as probe
from services.factory_sim.engine import evolve
from tests.test_checker import at, example_assignments, example_snapshot, make_candidate


def source_result(state, *, code=None, receipt=True):
    return {"state": state, "code": code, "receipt": receipt}


RACED = source_result("REJECTED", code="SOURCE_CONDITIONS_CHANGED")
ACCEPTED = source_result("ACTIVE")
BINDING_CHANGED = (409, "VALIDATION_BINDING_CHANGED")


class PublicationAPI:
    def __init__(self, scenario, candidate, outcomes):
        self.scenario, self.candidate = scenario, candidate
        self.outcomes = list(outcomes)
        self.requests = []
        self.created = []
        self.delivered = []
        self.certificates = []
        self.approved = []
        self.current = evolve(example_snapshot(), snapshot_clock=at(1))

    def request(self, request):
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix(f"/api/factories/{self.scenario.factory}")
        record = {
            "method": request.method,
            "path": path,
            "body": body,
            "role": request.headers["x-test-role"],
        }
        self.requests.append(record)
        if request.method == "POST":
            saved = json.loads(self.scenario.evidence.path.read_text(encoding="utf-8"))
            assert saved["last_operator_request"] == {
                "path": path,
                "request_id": body["request_id"],
                "payload_hash": canonical_hash(body),
            }
        if path.endswith(("/approvals", "/progress-approvals")):
            self.approved.append(deepcopy(record))
            return httpx.Response(200, json={"approval_id": f"approval-{len(self.approved)}"})
        if path.endswith("/validations"):
            identity = f"certificate-{len(self.certificates) + 1}"
            self.certificates.append({"certificate_id": identity, **deepcopy(record)})
            return httpx.Response(200, json={"certificate_id": identity})
        if path.endswith("/publications"):
            assert self.outcomes, "No further publication is authorized by this test"
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, tuple):
                return httpx.Response(outcome[0], json={"code": outcome[1]})
            release = {
                "release_id": f"release-{len(self.created) + 1}",
                "operation_id": body["request_id"],
                "candidate_hash": self.candidate["content_hash"],
                "source_state": "PENDING_SOURCE",
                "source_receipt_id": None,
            }
            self.created.append(
                {"request": deepcopy(record), "release": release, "outcome": outcome}
            )
            if outcome == "timeout-after-commit":
                raise httpx.ReadTimeout("private transport details", request=request)
            return httpx.Response(200, json=release)
        if path == "/workspace":
            return httpx.Response(200, json={"publications": deepcopy(self.delivered)})
        raise AssertionError(f"Unexpected controlled endpoint: {request.method} {path}")

    def deliver(self, engine, reader, writer):
        assert engine is self.scenario.engine
        assert reader is self.scenario.reader and writer is self.scenario.writer
        pending = self.created[len(self.delivered)]
        outcome = pending["outcome"]
        assert isinstance(outcome, dict)
        result = deepcopy(pending["release"])
        result.update(
            source_state=outcome["state"],
            source_receipt_id=f"receipt-{result['release_id']}" if outcome["receipt"] else None,
        )
        self.delivered.append({"release": result, "error_code": outcome["code"]})
        if outcome["state"] == "ACTIVE" and outcome["receipt"]:
            self.current = evolve(
                self.current,
                active_plan_version="accepted-plan",
                active_plan_hash=self.candidate["content_hash"],
            )
        return True

    def publication_requests(self):
        return [row for row in self.requests if row["path"].endswith("/publications")]

    def saved(self):
        return json.loads(self.scenario.evidence.path.read_text(encoding="utf-8"))


@pytest.fixture
def setup_probe(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Pure recovery tests must not connect to PostgreSQL, a model, or the network")

    monkeypatch.setattr(probe, "connect", forbidden)
    monkeypatch.setattr(probe, "configured_model", forbidden)
    monkeypatch.setattr(probe.socket.socket, "connect", forbidden)
    monkeypatch.setattr(probe, "code_hashes", lambda: {"controlled-test": "hash"})
    clients = []

    def setup(outcomes, *, overtime=True):
        original = example_snapshot()
        candidate = make_candidate(
            original, example_assignments(shift=5), allow_overtime=overtime
        ).model_dump(mode="json")
        evidence = probe.Evidence(tmp_path / "evidence.json", "controlled-model")
        evidence.data.update(
            requests_started=7, model_decisions=[{"request": 7, "state": "RETURNED"}]
        )
        evidence.save()
        scenario = probe.Scenario(probe.ProbeSettings(_env_file=None), evidence)
        api = PublicationAPI(scenario, candidate, outcomes)
        scenario.engine, scenario.writer = object(), object()
        scenario.reader = SimpleNamespace(snapshot=lambda factory: api.current)
        for role in ("planner", "manager"):
            client = httpx.Client(
                base_url="https://controlled.invalid",
                headers={"X-Test-Role": role},
                transport=httpx.MockTransport(api.request),
            )
            clients.append(client)
            scenario.clients[role] = client
        monkeypatch.setattr(probe, "deliver_one", api.deliver)
        return scenario, candidate, api

    yield setup
    for client in clients:
        client.close()


def assert_original_approvals_only(api):
    assert len(api.approved) == 2
    assert [(row["role"], row["body"]["action_scope"]) for row in api.approved] == [
        ("planner", "publish_plan"),
        ("manager", "allow_overtime"),
    ]
    assert all(row["body"]["decision"] == "APPROVED" for row in api.approved)
    assert all(
        row["body"]["candidate_hash"] == api.candidate["content_hash"] for row in api.approved
    )
    saved = api.saved()
    assert saved["requests_started"] == 7
    assert saved["max_requests"] == 40 and saved["requests_per_turn"] == 4
    assert saved["retries"] == saved["real_emails"] == 0
    assert saved["model_decisions"] == [{"request": 7, "state": "RETURNED"}]


def test_known_source_rejection_uses_new_certificate_and_operation_with_original_approvals(
    setup_probe,
):
    scenario, candidate, api = setup_probe([RACED, ACCEPTED])
    result = scenario.publish(candidate)
    assert result["source_state"] == "ACTIVE" and result["release_id"] == "release-2"
    assert len(api.created) == len(api.delivered) == len(api.certificates) == 2
    first, second = api.publication_requests()
    assert first["body"]["request_id"] != second["body"]["request_id"]
    assert first["body"]["certificate_id"] == "certificate-1"
    assert second["body"]["certificate_id"] == "certificate-2"
    assert len({row["body"]["request_id"] for row in api.certificates}) == 2
    assert all(
        row["body"]["candidate_hash"] == candidate["content_hash"]
        for row in api.publication_requests()
    )
    saved = api.saved()
    assert saved["publication_attempts"] == api.delivered
    assert saved["publication_attempts"][0]["release"]["source_state"] == "REJECTED"
    assert saved["releases"] == [result]
    assert_original_approvals_only(api)


def test_known_commit_binding_race_revalidates_without_creating_an_unaccepted_publication(
    setup_probe,
):
    scenario, candidate, api = setup_probe([BINDING_CHANGED, ACCEPTED])
    result = scenario.publish(candidate)
    assert result["source_state"] == "ACTIVE"
    assert len(api.publication_requests()) == len(api.certificates) == 2
    assert len(api.created) == len(api.delivered) == 1
    old, new = api.publication_requests()
    assert old["body"]["request_id"] != new["body"]["request_id"]
    assert old["body"]["certificate_id"] != new["body"]["certificate_id"]
    saved = api.saved()
    assert saved["publication_recovery"] == [
        {
            "attempt": 1,
            "request_id": old["body"]["request_id"],
            "error_code": "VALIDATION_BINDING_CHANGED",
        }
    ]
    assert saved["last_api_failure"] == {"status": 409, "code": "VALIDATION_BINDING_CHANGED"}
    assert saved["publication_attempts"] == api.delivered
    assert_original_approvals_only(api)


def test_last_allowed_attempt_can_succeed_after_both_api_and_source_races(setup_probe):
    scenario, candidate, api = setup_probe([BINDING_CHANGED, RACED, ACCEPTED])
    result = scenario.publish(candidate)
    assert result["source_state"] == "ACTIVE"
    assert len(api.publication_requests()) == len(api.certificates) == 3
    assert len(api.created) == len(api.delivered) == 2
    assert api.created[-1]["request"]["body"]["certificate_id"] == "certificate-3"
    assert len({row["body"]["request_id"] for row in api.publication_requests()}) == 3
    saved = api.saved()
    assert len(saved["publication_recovery"]) == 1
    assert [row["release"]["source_state"] for row in saved["publication_attempts"]] == [
        "REJECTED",
        "ACTIVE",
    ]
    assert saved["releases"] == [result]
    assert_original_approvals_only(api)


@pytest.mark.parametrize(
    "outcomes", [[RACED] * 3, [BINDING_CHANGED] * 3, [BINDING_CHANGED, RACED, RACED]]
)
def test_known_race_recovery_stops_at_three_total_attempts_and_retains_each_outcome(
    setup_probe, outcomes
):
    scenario, candidate, api = setup_probe(outcomes)
    with pytest.raises(probe.ProbeError, match="PUBLICATION_RECOVERY_BUDGET_EXHAUSTED"):
        scenario.publish(candidate)
    assert len(api.publication_requests()) == len(api.certificates) == 3
    assert len({row["body"]["request_id"] for row in api.publication_requests()}) == 3
    assert len(api.created) == len(api.delivered) == sum(isinstance(row, dict) for row in outcomes)
    saved = api.saved()
    assert saved.get("publication_attempts", []) == api.delivered
    assert len(saved.get("publication_recovery", [])) == sum(
        isinstance(row, tuple) for row in outcomes
    )
    assert "releases" not in saved
    assert_original_approvals_only(api)


@pytest.mark.parametrize(
    "outcome",
    [
        source_result("UNKNOWN", code="SOURCE_RESULT_UNKNOWN", receipt=False),
        source_result("REJECTED", code="SOURCE_CONDITIONS_CHANGED", receipt=False),
        source_result("REJECTED", code="CHECK_FAILED"),
        source_result("REJECTED", code="APPROVAL_REQUIRED"),
        source_result("ACTIVE", receipt=False),
    ],
)
def test_unknown_or_other_source_outcome_never_creates_a_replacement(setup_probe, outcome):
    scenario, candidate, api = setup_probe([outcome])
    with pytest.raises(probe.ProbeError, match="SOURCE_ACCEPTANCE_NOT_CONFIRMED"):
        scenario.publish(candidate)
    assert len(api.publication_requests()) == len(api.created) == len(api.certificates) == 1
    assert len(api.delivered) == 1
    saved = api.saved()
    assert saved["publication_attempts"] == api.delivered
    assert "releases" not in saved and "publication_recovery" not in saved
    assert_original_approvals_only(api)


@pytest.mark.parametrize(
    "failure",
    [(409, "UNRESOLVED_PUBLICATION"), (403, "FORBIDDEN"), (500, "VALIDATION_BINDING_CHANGED")],
)
def test_other_api_errors_preserve_original_request_without_new_certificate(setup_probe, failure):
    scenario, candidate, api = setup_probe([failure])
    with pytest.raises(probe.APIRejection) as error:
        scenario.publish(candidate)
    assert (error.value.status, error.value.api_code) == failure
    assert len(api.publication_requests()) == len(api.certificates) == 1
    assert api.created == api.delivered == []
    saved = api.saved()
    request = api.publication_requests()[0]
    assert saved["last_operator_request"]["request_id"] == request["body"]["request_id"]
    assert saved["last_operator_request"]["payload_hash"] == canonical_hash(request["body"])
    assert "publication_recovery" not in saved
    assert_original_approvals_only(api)


def test_http_timeout_after_local_commit_does_not_resend_and_preserves_recovery_identity(
    setup_probe,
):
    scenario, candidate, api = setup_probe(["timeout-after-commit"])
    with pytest.raises(httpx.ReadTimeout):
        scenario.publish(candidate)
    assert len(api.publication_requests()) == len(api.created) == len(api.certificates) == 1
    assert api.delivered == []
    request = api.created[0]["request"]
    saved = api.saved()
    assert saved["last_operator_request"] == {
        "path": request["path"],
        "request_id": request["body"]["request_id"],
        "payload_hash": canonical_hash(request["body"]),
    }
    assert "private transport details" not in scenario.evidence.path.read_text(encoding="utf-8")
    assert "releases" not in saved and "publication_recovery" not in saved
    assert_original_approvals_only(api)


def test_initial_plan_does_not_use_progress_recovery_or_certificates(setup_probe):
    scenario, candidate, api = setup_probe([RACED])
    with pytest.raises(probe.ProbeError, match="SOURCE_ACCEPTANCE_NOT_CONFIRMED"):
        scenario.publish(candidate, initial=True)
    assert len(api.publication_requests()) == len(api.created) == len(api.delivered) == 1
    assert api.certificates == []
    assert "certificate_id" not in api.publication_requests()[0]["body"]
    assert all(row["path"].endswith("/approvals") for row in api.approved)
    assert_original_approvals_only(api)
