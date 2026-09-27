import json
import logging

import pytest

from app.request_logging import logger as request_logger

TENANT_A = {"X-Tenant-Id": "tenant-a"}


@pytest.fixture
def request_logs(caplog):
    caplog.set_level(logging.INFO, logger=request_logger.name)

    def lines():
        return [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == request_logger.name
        ]

    return lines


def test_logs_one_json_line_per_request(client, request_logs):
    resp = client.get("/health", headers=TENANT_A)
    assert resp.status_code == 200

    logs = request_logs()
    assert len(logs) == 1
    entry = logs[0]
    assert set(entry) == {
        "request_id",
        "method",
        "path",
        "status",
        "latency_ms",
        "tenant_id",
    }
    assert entry["method"] == "GET"
    assert entry["path"] == "/health"
    assert entry["status"] == 200
    assert entry["tenant_id"] == "tenant-a"
    assert isinstance(entry["latency_ms"], (int, float))
    assert entry["latency_ms"] >= 0
    assert entry["request_id"] == resp.headers["X-Request-Id"]


def test_generates_distinct_request_ids(client, request_logs):
    first = client.get("/health").headers["X-Request-Id"]
    second = client.get("/health").headers["X-Request-Id"]
    assert first and second and first != second
    assert [e["request_id"] for e in request_logs()] == [first, second]


def test_honours_incoming_request_id(client, request_logs):
    resp = client.get("/health", headers={"X-Request-Id": "abc-123.def_4"})
    assert resp.headers["X-Request-Id"] == "abc-123.def_4"
    assert request_logs()[0]["request_id"] == "abc-123.def_4"


def test_rejects_malformed_incoming_request_id(client, request_logs):
    bad = "x" * 500
    resp = client.get("/health", headers={"X-Request-Id": bad})
    rid = resp.headers["X-Request-Id"]
    assert rid != bad
    assert request_logs()[0]["request_id"] == rid


def test_missing_tenant_logged_as_null(client, request_logs):
    client.get("/health")
    assert request_logs()[0]["tenant_id"] is None


def test_logs_error_status(client, fake_db, request_logs):
    resp = client.get("/items/NOPE", headers=TENANT_A)
    assert resp.status_code == 404
    entry = request_logs()[0]
    assert entry["status"] == 404
    assert entry["path"] == "/items/NOPE"
    assert resp.headers["X-Request-Id"] == entry["request_id"]


def test_never_logs_request_body(client, fake_db, request_logs, caplog):
    fake_db.add_item(
        sku="SECRETSKU",
        name="Widget",
        warehouse_id="w1",
        quantity=5,
        tenant_id="tenant-a",
    )
    resp = client.post(
        "/items/reserve",
        headers=TENANT_A,
        json={
            "sku": "SECRETSKU",
            "warehouse_id": "w1",
            "quantity": 1,
            "order_id": "order-body-marker",
        },
    )
    assert resp.status_code == 200
    entry = request_logs()[0]
    assert entry["method"] == "POST"
    assert entry["path"] == "/items/reserve"
    assert entry["status"] == 200
    assert "SECRETSKU" not in caplog.text
    assert "order-body-marker" not in caplog.text


def test_query_string_not_logged(client, fake_db, request_logs, caplog):
    client.get("/items?name=sensitive-query", headers=TENANT_A)
    assert request_logs()[0]["path"] == "/items"
    assert "sensitive-query" not in caplog.text
