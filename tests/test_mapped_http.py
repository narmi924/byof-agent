"""Registered HTTP mappings retain canonical facts and fail closed on scope or chain changes."""

from copy import deepcopy
from datetime import UTC, datetime

import httpx
import pytest

from packages.domain.execution import ActionReceipt
from packages.domain.models import ConnectorMapping, Snapshot, batch_operations
from packages.domain.skf import load_skf_snapshot
from packages.domain.snapshot_delta import apply_delta
from packages.integrations.factory_http import ConnectorError, FactoryHTTP
from packages.integrations.mapped_http import MappedFactoryHTTP
from services.factory_sim.engine import advance
from tests.test_case_impact import active_case
from tests.test_checker import change_snapshot, example_snapshot
from tests.test_connector_mapping import change_batch, mapping_for, wire_row, wire_snapshot


def registered_mapping(snapshot, *, alternate=True, operations=None):
    original = mapping_for(snapshot, alternate=alternate).model_dump()
    names = {
        "read_snapshot": "snapshot",
        "read_changes": "changes",
        "query_detail": "detail",
        "query_action": "action",
        "accept_plan": "plans",
    }
    original["endpoint_bindings"] = [
        {"operation": operation, "endpoint_id": f"{'alternate' if alternate else 'v1'}_{name}"}
        for operation, name in names.items()
        if operations is None or operation in operations
    ]
    return ConnectorMapping.model_validate(original)


@pytest.fixture
def reader():
    clients = []

    def create(snapshot, response, *, alternate=True, mapping=None, **options):
        requests = []

        def handle(request):
            requests.append(request)
            assert request.headers["authorization"] == "Bearer reader-test-token"
            if callable(response):
                return response(request)
            return httpx.Response(200, json=response)

        client = MappedFactoryHTTP(
            options.pop("origin", "https://registered.invalid"),
            "reader-test-token",
            factory_id=options.pop("factory_id", snapshot.factory_id),
            origin_id=options.pop("origin_id", "registered-enterprise"),
            mapping=mapping or registered_mapping(snapshot, alternate=alternate),
            transport=httpx.MockTransport(handle),
            **options,
        )
        clients.append(client)
        return client, requests

    yield create
    for client in clients:
        client.close()


@pytest.mark.parametrize("alternate", [False, True])
def test_full_skf_http_fields_units_and_statuses_produce_identical_canonical_facts(
    reader, alternate
):
    original = load_skf_snapshot()
    source = change_snapshot(
        original, lambda raw: raw["source"].update(source_revision="1", cursor="1")
    )
    wire = wire_snapshot(source) if alternate else source.model_dump(mode="json")
    unchanged = deepcopy(wire)
    client, requests = reader(source, wire, alternate=alternate)
    result = client.snapshot(source.factory_id)
    assert isinstance(client, FactoryHTTP)
    assert result == source and result.content_hash == source.content_hash
    batches, operations = batch_operations(result)
    assert (
        len(result.orders),
        sum(order.quantity for order in result.orders),
        len(batches),
        len(operations),
    ) == (6, 5400, 108, 864)
    assert result.profile == original.profile and result.horizon == original.horizon
    assert wire == unchanged
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path == f"/factory/{'v2' if alternate else 'v1'}/snapshot"
    assert dict(requests[0].url.params) == {"factory_id": source.factory_id}


@pytest.mark.parametrize(
    "change", ["hash", "factory", "source", "stale", "incomplete", "consistency", "cursor"]
)
def test_snapshot_scope_hash_and_source_guarantees_are_required(reader, change):
    source = example_snapshot()
    raw = source.model_dump(mode="json", exclude={"content_hash"})
    if change == "factory":
        raw["factory_id"] = raw["profile"]["factory_id"] = "other-factory"
    elif change == "source":
        raw["source"]["source_system"] = "other-source"
    elif change == "stale":
        raw["source"]["freshness"] = "STALE"
    elif change == "incomplete":
        raw["source"]["complete"] = False
    elif change == "consistency":
        raw["source"]["consistency"] = "UNVERIFIED"
    elif change == "cursor":
        raw["source"]["cursor"] = "2"
    wire = wire_snapshot(Snapshot.model_validate(raw))
    if change == "hash":
        wire["content_hash"] = "f" * 64
    client, requests = reader(source, wire)
    with pytest.raises(ConnectorError):
        client.snapshot(source.factory_id)
    assert len(requests) == 1


def page(before, after, changes, *, has_more=False, watermark=None):
    return {
        "factory_id": before.factory_id,
        "run_id": before.run_id,
        "watermark": watermark or after.source.source_revision,
        "next_cursor": after.source.source_revision,
        "has_more": has_more,
        "changes": changes,
    }


@pytest.mark.parametrize("alternate", [False, True])
def test_http_change_pages_normalize_each_real_transition_and_preserve_pagination(
    reader, alternate
):
    before, plan = active_case()
    first = advance(before, plan)
    second = advance(first, plan)
    batches = [
        change_batch(before, first, alternate=alternate),
        change_batch(first, second, alternate=alternate),
    ]
    pages = [
        page(before, first, batches[:1], has_more=True, watermark=second.source.source_revision),
        page(first, second, batches[1:]),
        page(second, second, []),
    ]
    saved = deepcopy(pages)

    def respond(request):
        assert int(request.url.params["limit"]) == 1
        assert request.url.params["run_id"] == before.run_id
        return httpx.Response(200, json=pages.pop(0))

    client, requests = reader(before, respond, alternate=alternate)
    one = client.changes(before, limit=1)
    assert one["has_more"] is True and one["next_cursor"] == first.source.source_revision
    assert one["watermark"] == second.source.source_revision
    assert one["changes"] == [change_batch(before, first)]
    normalized_first = apply_delta(before, one["changes"][0]["snapshot_delta"])
    assert normalized_first == first
    two = client.changes(normalized_first, limit=1)
    assert two["has_more"] is False and two["changes"] == [change_batch(first, second)]
    normalized_second = apply_delta(normalized_first, two["changes"][0]["snapshot_delta"])
    assert normalized_second == second
    assert client.changes(normalized_second, limit=1) == saved[2]
    assert [request.url.params["after"] for request in requests] == [
        before.source.source_revision,
        first.source.source_revision,
        second.source.source_revision,
    ]
    assert all(
        request.url.path == f"/factory/{'v2' if alternate else 'v1'}/changes"
        for request in requests
    )
    assert before.inventory[0].reserved == 0 and second.inventory[0].reserved == 2


@pytest.mark.parametrize(
    "variant",
    [
        "foreign_factory",
        "foreign_run",
        "missing_batch",
        "duplicate_batch",
        "reordered",
        "wrong_hash",
        "missing_delta",
        "watermark_behind",
        "cursor_ahead",
        "cursor_behind",
        "padded_cursor",
        "unicode_cursor",
        "integer_cursor",
        "wrong_more",
        "nonboolean_more",
        "too_many",
        "unexpected_field",
    ],
)
def test_malformed_change_chain_or_page_cannot_advance_cursor(reader, variant):
    before, plan = active_case()
    first, second = advance(before, plan), advance(before, plan, minutes=2)
    batches = [
        change_batch(before, first, alternate=True),
        change_batch(first, second, alternate=True),
    ]
    raw = page(before, second, batches)
    limit = 100
    if variant == "foreign_factory":
        raw["factory_id"] = "foreign"
    elif variant == "foreign_run":
        raw["run_id"] = "another-run"
    elif variant == "missing_batch":
        raw["changes"] = batches[1:]
    elif variant == "duplicate_batch":
        raw["changes"] = [batches[0], batches[0]]
    elif variant == "reordered":
        raw["changes"].reverse()
    elif variant == "wrong_hash":
        raw["changes"][0]["snapshot_hash"] = "f" * 64
    elif variant == "missing_delta":
        raw["changes"][0].pop("snapshot_delta")
    elif variant == "watermark_behind":
        raw["watermark"] = before.source.source_revision
    elif variant == "cursor_ahead":
        raw["next_cursor"] = raw["watermark"] = str(int(second.source.source_revision) + 1)
    elif variant == "cursor_behind":
        raw["next_cursor"] = before.source.source_revision
    elif variant == "padded_cursor":
        raw["next_cursor"] = "0" + raw["next_cursor"]
    elif variant == "unicode_cursor":
        raw["next_cursor"] = "٤"
    elif variant == "integer_cursor":
        raw["next_cursor"] = int(raw["next_cursor"])
    elif variant == "wrong_more":
        raw["has_more"] = True
    elif variant == "nonboolean_more":
        raw["has_more"] = 0
    elif variant == "too_many":
        limit = 1
    else:
        raw["future_events"] = []
    client, requests = reader(before, raw)
    with pytest.raises(ConnectorError):
        client.changes(before, limit=limit)
    assert len(requests) == 1


def test_empty_page_cannot_hide_a_declared_backlog(reader):
    before, _ = active_case()
    raw = page(
        before, before, [], has_more=True, watermark=str(int(before.source.source_revision) + 1)
    )
    client, _ = reader(before, raw)
    with pytest.raises(ConnectorError, match="contract") as failure:
        client.changes(before)
    assert failure.value.code == "CHANGE_PAGE_CURSOR_MISMATCH"


@pytest.mark.parametrize("limit", [True, False, 0, 101, 1.5, "2"])
def test_invalid_paging_limit_makes_no_http_request(reader, limit):
    before, _ = active_case()
    client, requests = reader(before, {})
    with pytest.raises(ConnectorError) as failure:
        client.changes(before, limit=limit)
    assert failure.value.code == "INVALID_CHANGE_LIMIT" and requests == []


def test_detail_uses_bound_route_and_maps_only_the_requested_canonical_record(reader):
    source = example_snapshot()
    order = source.orders[0].model_dump(mode="json")
    raw = {
        "record": wire_row("order", order),
        "source": source.source.model_dump(mode="json"),
        "run_id": source.run_id,
    }
    client, requests = reader(source, raw)
    result = client.query_detail(source.factory_id, source.run_id, "orders", order["order_id"])
    assert result == {**raw, "record": order}
    assert requests[0].url.path == "/factory/v2/objects/orders/order-a"
    assert dict(requests[0].url.params) == {"factory_id": source.factory_id}


def test_actual_execution_detail_keeps_the_standard_ledger_without_remapping_consumption(reader):
    before, plan = active_case()
    source = advance(before, plan)
    actual = source.actuals[0]
    raw = {
        "record": actual.model_dump(mode="json"),
        "source": source.source.model_dump(mode="json"),
        "run_id": source.run_id,
    }
    client, requests = reader(source, raw)
    result = client.query_detail(source.factory_id, source.run_id, "actuals", actual.operation_id)
    assert result == raw
    assert requests[0].url.path.endswith("/actuals/" + actual.operation_id)


@pytest.mark.parametrize("variant", ["run", "source", "identity", "stale", "extra"])
def test_detail_response_must_match_requested_run_source_identity_and_contract(reader, variant):
    source = example_snapshot()
    raw = {
        "record": wire_row("order", source.orders[0].model_dump(mode="json")),
        "source": source.source.model_dump(mode="json"),
        "run_id": source.run_id,
    }
    if variant == "run":
        raw["run_id"] = "other-run"
    elif variant == "source":
        raw["source"]["source_system"] = "other-source"
    elif variant == "identity":
        raw["record"]["identity"]["number"] = "other-order"
    elif variant == "stale":
        raw["source"]["freshness"] = "STALE"
    else:
        raw["control_token"] = "must-not-leave-response"
    client, _ = reader(source, raw)
    with pytest.raises(ConnectorError):
        client.query_detail(source.factory_id, source.run_id, "orders", source.orders[0].order_id)


def receipt(source, operation_id="publication:original"):
    return {
        "operation_id": operation_id,
        "factory_id": source.factory_id,
        "run_id": source.run_id,
        "receipt_id": "received-action",
        "candidate_hash": "a" * 64,
        "source_state": "REJECTED",
        "plan_version": None,
        "effective_at": None,
        "recorded_at": datetime.now(UTC).isoformat(),
        "error_code": "SOURCE_CONDITIONS_CHANGED",
    }


@pytest.mark.parametrize("found", [False, True])
def test_original_action_lookup_remains_available_without_any_write_capability(reader, found):
    source = example_snapshot()
    raw = receipt(source)
    client, requests = reader(
        source, lambda _: httpx.Response(200, json=raw) if found else httpx.Response(404)
    )
    result = client.action(source.factory_id, source.run_id, raw["operation_id"])
    assert (result is not None) is found
    if result is not None:
        assert result == ActionReceipt.model_validate(raw)
    assert requests[0].url.raw_path.split(b"?")[0] == b"/factory/v2/actions/publication%3Aoriginal"
    assert not hasattr(client, "submit") and not hasattr(client, "command")


@pytest.mark.parametrize("field", ["factory_id", "run_id", "operation_id"])
def test_action_receipt_cannot_refer_to_another_original_operation(reader, field):
    source = example_snapshot()
    raw = receipt(source)
    raw[field] = "different"
    client, _ = reader(source, raw)
    with pytest.raises(ConnectorError) as failure:
        client.action(source.factory_id, source.run_id, "publication:original")
    assert failure.value.code == "ACTION_RECEIPT_SCOPE_MISMATCH"


def test_capabilities_do_not_advertise_unbound_or_reader_only_writes(reader):
    source = example_snapshot()
    raw = {
        "read_snapshot": True,
        "read_changes": True,
        "query_detail": True,
        "accept_plan": True,
        "query_action": True,
        "idempotency": True,
        "conditional_acceptance": True,
        "snapshot_consistency": "ATOMIC_SNAPSHOT",
    }
    mapping = registered_mapping(
        source, operations={"read_snapshot", "query_action", "accept_plan"}
    )
    client, requests = reader(source, raw, mapping=mapping)
    supported = client.capabilities()
    assert supported.read_snapshot and supported.query_action
    assert not supported.read_changes and not supported.query_detail
    assert (
        not supported.accept_plan
        and not supported.conditional_acceptance
        and not supported.idempotency
    )
    assert supported.snapshot_consistency == "ATOMIC_SNAPSHOT"
    assert requests[0].url.path == "/factory/v1/capabilities"


@pytest.mark.parametrize("operation", ["changes", "action", "detail"])
def test_unbound_optional_operation_does_not_fall_back_to_legacy_http(reader, operation):
    source = example_snapshot()
    mapping = registered_mapping(source, operations={"read_snapshot"})
    client, requests = reader(source, {}, mapping=mapping)
    with pytest.raises(ConnectorError) as failure:
        if operation == "changes":
            client.changes(source)
        elif operation == "action":
            client.action(source.factory_id, source.run_id, "original")
        else:
            client.query_detail(source.factory_id, source.run_id, "orders", "order-a")
    assert failure.value.code == "MAPPED_OPERATION_UNAVAILABLE"
    assert requests == []


@pytest.mark.parametrize("problem", ["missing_fields", "ambiguous_path", "snapshot_unbound"])
def test_incomplete_mapping_is_rejected_before_any_source_request(reader, problem):
    source = example_snapshot()
    raw = registered_mapping(source).model_dump(mode="json")
    if problem == "missing_fields":
        raw["fields"].pop()
    elif problem == "ambiguous_path":
        raw["fields"][1]["source_field"] = raw["fields"][0]["source_field"]
    else:
        raw["endpoint_bindings"] = [
            row for row in raw["endpoint_bindings"] if row["operation"] != "read_snapshot"
        ]
    with pytest.raises(ConnectorError) as failure:
        reader(source, {}, mapping=ConnectorMapping.model_validate(raw))
    assert (
        failure.value.code
        == {
            "missing_fields": "INCOMPLETE_FIELD_MAPPING",
            "ambiguous_path": "AMBIGUOUS_MAPPING_PATH",
            "snapshot_unbound": "SNAPSHOT_ENDPOINT_REQUIRED",
        }[problem]
    )


@pytest.mark.parametrize("problem", ["factory", "source", "hash"])
def test_invalid_or_foreign_before_snapshot_does_not_send_an_incremental_request(reader, problem):
    source = example_snapshot()
    raw = source.model_dump(mode="json", exclude={"content_hash"})
    if problem == "factory":
        raw["factory_id"] = raw["profile"]["factory_id"] = "another-factory"
    elif problem == "source":
        raw["source"]["source_system"] = "another-source"
    if problem == "hash":
        before = source.model_copy(update={"content_hash": "f" * 64})
    else:
        before = Snapshot.model_validate(raw)
    client, requests = reader(source, {})
    with pytest.raises(ConnectorError):
        client.changes(before)
    assert requests == []


@pytest.mark.parametrize("endpoint", ["unknown", "v1_plans", "%2Fsimulator", "alternate_control"])
def test_unknown_encoded_control_or_wrong_operation_endpoint_is_rejected_at_construction(
    reader, endpoint
):
    source = example_snapshot()
    raw = registered_mapping(source).model_dump()
    raw["endpoint_bindings"][0]["endpoint_id"] = endpoint
    with pytest.raises(ConnectorError) as failure:
        reader(source, {}, mapping=ConnectorMapping.model_validate(raw))
    assert failure.value.code == "UNREGISTERED_MAPPING_ENDPOINT"


@pytest.mark.parametrize(
    "origin",
    [
        "http://remote.invalid",
        "https://registered.invalid/other",
        "https://user:private@registered.invalid",
        "file:///private",
        "https://registered.invalid?next=other",
    ],
)
def test_configured_origin_cannot_carry_paths_credentials_or_unsafe_protocols(reader, origin):
    with pytest.raises(ConnectorError) as failure:
        reader(example_snapshot(), {}, origin=origin)
    assert "private" not in str(failure.value)


def test_mapping_origin_and_factory_must_match_trusted_constructor_binding(reader):
    source = example_snapshot()
    with pytest.raises(ConnectorError) as origin:
        reader(source, {}, origin_id="another-registered-origin")
    assert origin.value.code == "MAPPING_ORIGIN_MISMATCH"
    with pytest.raises(ConnectorError) as factory:
        reader(source, {}, factory_id="foreign-factory")
    assert factory.value.code == "MAPPING_FACTORY_MISMATCH"


@pytest.mark.parametrize(
    "identity",
    [
        "..",
        ".",
        "../commands",
        "a/b",
        r"a\b",
        "%2Fcommands",
        "%252Fcommands",
        "x?factory_id=other",
        "x#other",
        "line\nfeed",
    ],
)
def test_path_and_double_encoded_identities_never_reach_http(reader, identity):
    source = example_snapshot()
    client, requests = reader(source, {})
    with pytest.raises(ConnectorError):
        client.action(source.factory_id, source.run_id, identity)
    with pytest.raises(ConnectorError):
        client.query_detail(source.factory_id, source.run_id, "orders", identity)
    assert requests == []


def test_bound_reader_rejects_other_factory_unknown_entities_and_unwired_legacy_calls(reader):
    source = example_snapshot()
    client, requests = reader(source, {})
    for call in (
        lambda: client.snapshot("other-factory"),
        lambda: client.action("other-factory", source.run_id, "original"),
        lambda: client.query_detail(source.factory_id, source.run_id, "simulator", "control"),
        lambda: client._get("/factory/v1/changes", {"factory_id": source.factory_id}),
        lambda: client._get("https://unregistered.invalid/private"),
        lambda: client._request("POST", "/simulator/v1/factories/any/commands", body={}),
    ):
        with pytest.raises(ConnectorError):
            call()
    assert requests == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://unregistered.invalid/private"}),
        httpx.Response(200, content=b'{"content_hash":"a","content_hash":"b"}'),
        httpx.Response(200, content=b"[]"),
    ],
)
def test_redirect_and_ambiguous_json_never_produce_mapped_facts(reader, response):
    source = example_snapshot()
    client, requests = reader(source, lambda _: response)
    with pytest.raises(ConnectorError):
        client.snapshot(source.factory_id)
    assert len(requests) == 1 and requests[0].url.host == "registered.invalid"


def test_mapped_reader_retains_existing_response_size_limit(reader):
    source = example_snapshot()
    client, requests = reader(source, lambda _: httpx.Response(200, content=b" " * 5_000_001))
    with pytest.raises(ConnectorError, match="size limit"):
        client.snapshot(source.factory_id)
    assert len(requests) == 1
