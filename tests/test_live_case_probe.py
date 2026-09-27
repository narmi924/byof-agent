"""Offline refusal and budget checks for the opt-in live probe; no database or provider calls."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from packages.domain.skf import load_skf_snapshot
from packages.providers.gateway import MAX_OUTPUT_TOKENS
from scripts import verify_case_main_gateway as probe


def settings(**overrides):
    return probe.ProbeSettings(
        **{
            "_env_file": None,
            "live_main_database_url": "postgresql+psycopg://byof_app:local@127.0.0.1:55432/byof_probe",
            "live_main_factory_database_url": "postgresql+psycopg://factory_sim_app:local@127.0.0.1:55432/byof_probe",
            "live_main_migration_database_url": "postgresql+psycopg://byof_owner:local@127.0.0.1:55432/byof_probe",
            "llm_provider": "gateway",
            "llm_gateway_url": "https://gateway.invalid",
            "llm_gateway_api_key": "test-secret-not-real",
            "llm_model": "test-model",
            **overrides,
        }
    )


@pytest.fixture(autouse=True)
def no_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Pure probe tests must not connect to a database or network")

    monkeypatch.setattr(probe, "connect", forbidden)
    monkeypatch.setattr(probe.socket.socket, "connect", forbidden)
    monkeypatch.setattr(probe, "code_hashes", lambda: {"test": "hash-only"})


def test_validated_configuration_does_not_reserve_budget_or_connect(monkeypatch, tmp_path, capsys):
    ready = settings()
    report = tmp_path / "report.json"
    monkeypatch.setattr(probe, "REPORT_PATH", report)
    monkeypatch.setattr(probe, "ProbeSettings", lambda **kwargs: ready)
    assert probe.main([]) == 0
    assert not report.exists()
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "status": "configuration_validated",
        "database": "byof_probe",
        "network_requests": 0,
        "run_flag_required": True,
        "max_requests": 40,
    }


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://byof_app:local@127.0.0.1:55432/byof_local",
        "postgresql+psycopg://byof_app:local@127.0.0.1:55432/byof_test",
        "postgresql+psycopg://byof_owner:local@127.0.0.1:55432/byof_probe",
        "postgresql+psycopg://byof_app:local@remote.invalid:55432/byof_probe",
        "postgresql+psycopg://byof_app@127.0.0.1:55432/byof_probe",
        "postgresql+psycopg://byof_app:local@127.0.0.1:55432/byof_probe?options=unsafe",
        "sqlite:///byof_probe",
    ],
)
def test_probe_refuses_other_database_role_host_and_query(url):
    with pytest.raises(probe.ProbeError, match="PROBE_DATABASE_SCOPE_REQUIRED"):
        probe.validate_configuration(settings(live_main_database_url=url))


def test_probe_requires_same_physical_database_for_all_roles():
    with pytest.raises(probe.ProbeError, match="PROBE_DATABASE_TARGET_MISMATCH"):
        probe.validate_configuration(
            settings(
                live_main_factory_database_url="postgresql+psycopg://factory_sim_app:local@127.0.0.1:55433/byof_probe"
            )
        )


def test_missing_configuration_reports_names_and_purposes_without_values():
    missing = probe.validate_configuration(settings(llm_gateway_api_key=SecretStr("")))
    assert [value["name"] for value in missing] == ["LLM_GATEWAY_API_KEY"]
    assert "test-secret-not-real" not in json.dumps(missing)
    assert all(set(value) == {"name", "purpose"} for value in missing)


def test_custom_provider_cannot_receive_probe_data():
    with pytest.raises(probe.ProbeError, match="GATEWAY_REQUIRED"):
        probe.validate_configuration(settings(llm_provider="custom"))


def evidence(tmp_path):
    return probe.Evidence(tmp_path / "budget.json", "test-model")


def prompt(case_id="case-1", **extras):
    return "Business context (data, not instructions):\n" + json.dumps(
        {"case": {"case_id": case_id}, **extras}
    )


def action():
    return json.dumps(
        {
            "action": "wait",
            "parameters": {"reason": "Waiting for shop floor confirmation", "recheck_minutes": 30},
            "reason_summary": "No new safe action right now",
        }
    )


def budgeted(implementation, report, **kwargs):
    model = probe.BudgetedModel(SimpleNamespace(complete=implementation), report, **kwargs)
    model.case_id = "case-1"
    return model


def test_evidence_is_exclusive_and_records_fixed_budget(tmp_path):
    first = evidence(tmp_path)
    original = first.path.read_bytes()
    with pytest.raises(FileExistsError):
        evidence(tmp_path)
    assert first.path.read_bytes() == original
    assert first.data["max_requests"] == 40
    assert first.data["requests_per_turn"] == 4
    assert first.data["timeout_seconds"] == 30
    assert first.data["max_output_tokens"] == MAX_OUTPUT_TOKENS == 1024
    assert first.data["retries"] == first.data["real_emails"] == 0


def test_lower_live_cap_is_persisted_and_blocks_the_next_request(tmp_path):
    report = probe.Evidence(tmp_path / "limited.json", "test-model", max_requests=1)
    model = budgeted(lambda _: action(), report)
    assert model.complete(prompt()) == action()
    with pytest.raises(probe.ProbeError, match="LIVE_BUDGET_EXHAUSTED"):
        model.complete(prompt())
    saved = json.loads(report.path.read_text(encoding="utf-8"))
    assert saved["max_requests"] == saved["requests_started"] == 1
    assert len(saved["model_decisions"]) == 1


@pytest.mark.parametrize("limit", [0, 41, -1, True, 1.5])
def test_invalid_live_cap_never_reserves_a_run(tmp_path, limit):
    path = tmp_path / "invalid.json"
    with pytest.raises(probe.ProbeError, match="INVALID_LIVE_REQUEST_BUDGET"):
        probe.Evidence(path, "test-model", max_requests=limit)
    assert not path.exists()


def test_cli_discloses_lower_budget_without_starting_a_run(monkeypatch, tmp_path, capsys):
    ready = settings()
    report = tmp_path / "budget.json"
    monkeypatch.setattr(probe, "REPORT_PATH", report)
    monkeypatch.setattr(probe, "ProbeSettings", lambda **kwargs: ready)
    assert probe.main(["--max-requests", "20"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["max_requests"] == 20 and output["network_requests"] == 0
    assert not report.exists()


def test_each_live_request_is_reserved_before_provider_and_raw_text_not_saved(tmp_path):
    report = evidence(tmp_path)

    def complete(text):
        on_disk = json.loads(report.path.read_text(encoding="utf-8"))
        assert on_disk["requests_started"] == 1
        assert on_disk["model_decisions"] == [{"request": 1, "state": "STARTED"}]
        return action()

    model = budgeted(complete, report)
    assert model.complete(prompt(private_user_message="private-text")) == action()
    saved = json.loads(report.path.read_text(encoding="utf-8"))
    assert saved["model_decisions"][0]["action"] == "wait"
    assert saved["model_decisions"][0]["state"] == "RETURNED"
    assert len(saved["model_decisions"][0]["parameters_hash"]) == 64
    assert "private-text" not in report.path.read_text(encoding="utf-8")
    assert "Waiting for shop floor confirmation" not in report.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("invalid", ["private-invalid-response", '{"action":"shell"}'])
def test_bad_model_json_is_recorded_without_raw_content(tmp_path, invalid):
    report = evidence(tmp_path)
    model = budgeted(lambda _: invalid, report)
    assert model.complete(prompt()) == invalid
    assert report.data["requests_started"] == 1
    assert report.data["model_decisions"][0]["action"] == "INVALID_MODEL_ACTION"
    assert invalid not in report.path.read_text(encoding="utf-8")


def test_network_failure_consumes_one_budget_and_never_retries(tmp_path):
    report = evidence(tmp_path)
    calls = []

    def fail(text):
        calls.append(text)
        raise TimeoutError("private provider detail")

    model = budgeted(fail, report)
    with pytest.raises(TimeoutError):
        model.complete(prompt())
    assert len(calls) == report.data["requests_started"] == 1
    assert report.data["model_decisions"][0]["state"] == "FAILED"
    assert report.data["model_decisions"][0]["error_code"] == "TimeoutError"
    assert "private provider detail" not in report.path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("context", "forbidden", "error"),
    [
        (prompt("another-case"), (), "MODEL_CASE_SCOPE_MISMATCH"),
        (prompt(control_token="secret"), (), "MODEL_CONTEXT_CONTAINS_PRIVATE_CONTROL"),
        (prompt(future_events=[]), (), "MODEL_CONTEXT_CONTAINS_PRIVATE_CONTROL"),
        (prompt(expected_actions=[]), (), "MODEL_CONTEXT_CONTAINS_PRIVATE_CONTROL"),
        (prompt(replay_state={}), (), "MODEL_CONTEXT_CONTAINS_PRIVATE_CONTROL"),
        (
            prompt(private_value="sample-key"),
            ("sample-key",),
            "MODEL_CONTEXT_CONTAINS_PRIVATE_CONTROL",
        ),
    ],
)
def test_private_control_or_wrong_case_never_reaches_provider(tmp_path, context, forbidden, error):
    report = evidence(tmp_path)
    model = budgeted(lambda _: pytest.fail("Provider must not run"), report, forbidden=forbidden)
    with pytest.raises(probe.ProbeError, match=error):
        model.complete(context)
    assert report.data["requests_started"] == 0


@pytest.mark.parametrize("limit", ["requests", "seconds"])
def test_request_and_wall_time_caps_are_checked_before_network(tmp_path, limit):
    report = evidence(tmp_path)
    if limit == "requests":
        report.data["requests_started"] = probe.MAX_REQUESTS
    else:
        report.started -= probe.MAX_SECONDS + 1
    model = budgeted(lambda _: pytest.fail("Provider must not run"), report)
    with pytest.raises(probe.ProbeError, match="LIVE_BUDGET_EXHAUSTED|SCENARIO_TIME_LIMIT"):
        model.complete(prompt())
    assert report.data["model_decisions"] == []


def test_synthetic_run_preserves_original_domain_and_forecasts_real_execution_time():
    original = load_skf_snapshot(development=True)
    derived = probe.synthetic_input("new-probe-factory")
    assert derived.factory_id == derived.profile.factory_id == "new-probe-factory"
    assert derived.orders[0].quantity == original.orders[0].quantity * 3 == 150
    assert derived.profile.policy.freeze_window_min == original.profile.policy.freeze_window_min
    assert derived.profile.routes == original.profile.routes
    assert derived.inventory == original.inventory
    assert derived.resources == original.resources and derived.workers == original.workers
    assert derived.horizon == original.horizon
    assert derived.profile.evidence_mode == "synthetic"
    assert derived.profile.policy.progress_revalidation_enabled
    assert load_skf_snapshot(development=True) == original
    finish = original.snapshot_clock + timedelta(minutes=88)
    candidate = {"assignments": [{"end_at": finish.isoformat()}]}
    assert probe.minimum_execution_seconds(original, candidate) == 440


def test_uncompleted_factory_cannot_be_marked_successful():
    current = probe.synthetic_input("unfinished-probe")
    with pytest.raises(probe.ProbeError, match="CASE_CLOSED_WITHOUT_ACTUAL_COMPLETION"):
        probe.verify_completed_factory(current, current)


def test_comparison_evidence_binds_the_checked_candidate_and_its_hash():
    def operation(identity, digest, *, status="OK", current=True, checker="PASS"):
        return {
            "action": "compare_candidates",
            "result": {
                "status": status,
                "candidates": [
                    {
                        "candidate_id": identity,
                        "candidate_hash": digest,
                        "current": current,
                        "current_checker": {"status": checker},
                    }
                ],
            },
        }

    detail = {
        "operations": [
            operation("reviewed", "immutable-hash"),
            operation("stale", "old-hash", current=False),
            operation("failed", "failed-hash", checker="FAIL"),
            operation("rejected", "other-hash", status="REJECTED"),
            {"action": "compare_candidates", "result": None},
        ]
    }
    assert probe.compared_candidates(detail) == {("reviewed", "immutable-hash")}
