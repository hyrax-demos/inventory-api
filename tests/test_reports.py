import csv
import io
from datetime import datetime

import pytest

TENANT_A = {"X-Tenant-Id": "tenant-a"}


def test_low_stock_report_happy_path(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=2, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w1", quantity=99, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", params={"threshold": 10}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_low_stock_report_scoped_to_tenant(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w1", quantity=1, tenant_id="tenant-b"
    )
    resp = client.get("/reports/low-stock", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_low_stock_csv_smoke(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=2, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock.csv", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.text.splitlines()[0] == "sku,name,warehouse_id,quantity"


CSV_HEADER = ["sku", "name", "warehouse_id", "quantity"]


def _csv_rows(resp) -> list[dict]:
    """Parse a low-stock CSV response body with the stdlib csv module."""
    reader = csv.DictReader(io.StringIO(resp.text, newline=""))
    assert reader.fieldnames == CSV_HEADER
    return list(reader)


def test_low_stock_csv_content_type(client, fake_db):
    resp = client.get("/reports/low-stock.csv", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")


def test_low_stock_csv_header_row_with_no_items(client, fake_db):
    resp = client.get("/reports/low-stock.csv", headers=TENANT_A)
    assert resp.status_code == 200
    lines = resp.text.splitlines()
    assert lines == ["sku,name,warehouse_id,quantity"]


def test_low_stock_csv_header_row_with_items(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=2, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock.csv", headers=TENANT_A)
    assert resp.status_code == 200
    lines = resp.text.splitlines()
    assert lines[0] == "sku,name,warehouse_id,quantity"
    assert lines[1] == "A,a,w1,2"


TRICKY_NAMES = {
    "COMMA": "bolts, assorted",
    "QUOTE": 'a "b"',
    "NEWLINE": "line one\nline two",
    "CRLF": "line one\r\nline two",
    "ALL": 'x, "y"\nz',
}


def test_low_stock_csv_escaping_round_trips(client, fake_db):
    for i, (sku, name) in enumerate(TRICKY_NAMES.items()):
        fake_db.add_item(
            sku=sku, name=name, warehouse_id="w1", quantity=i, tenant_id="tenant-a"
        )
    resp = client.get("/reports/low-stock.csv", headers=TENANT_A)
    assert resp.status_code == 200
    rows = _csv_rows(resp)
    assert {r["sku"]: r["name"] for r in rows} == TRICKY_NAMES
    # Every row still has exactly the four columns, in order.
    assert [r["sku"] for r in rows] == list(TRICKY_NAMES)
    assert all(r["warehouse_id"] == "w1" for r in rows)


def test_low_stock_csv_escaping_uses_rfc4180_quoting(client, fake_db):
    fake_db.add_item(
        sku="QUOTE", name='a "b"', warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="COMMA", name="x,y", warehouse_id="w1", quantity=2, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="NL", name="p\nq", warehouse_id="w1", quantity=3, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock.csv", headers=TENANT_A)
    assert resp.status_code == 200
    text = resp.text
    assert 'QUOTE,"a ""b""",w1,1' in text
    assert 'COMMA,"x,y",w1,2' in text
    assert 'NL,"p\nq",w1,3' in text


def test_low_stock_csv_threshold_matches_json(client, fake_db):
    # Below, at, and above the threshold (boundary is the "at" item).
    fake_db.add_item(
        sku="BELOW", name="below", warehouse_id="w1", quantity=4, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="AT", name="at", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="ABOVE", name="above", warehouse_id="w1", quantity=6, tenant_id="tenant-a"
    )
    params = {"threshold": 5}
    json_resp = client.get("/reports/low-stock", params=params, headers=TENANT_A)
    csv_resp = client.get("/reports/low-stock.csv", params=params, headers=TENANT_A)
    assert json_resp.status_code == csv_resp.status_code == 200

    json_skus = [i["sku"] for i in json_resp.json()["items"]]
    csv_rows = _csv_rows(csv_resp)
    # Same rows, same order as the JSON endpoint.
    assert [r["sku"] for r in csv_rows] == json_skus
    # JSON semantics are "at or below": the boundary item is included.
    assert json_skus == ["BELOW", "AT"]
    assert "ABOVE" not in [r["sku"] for r in csv_rows]
    assert [int(r["quantity"]) for r in csv_rows] == [
        i["quantity"] for i in json_resp.json()["items"]
    ]


def test_low_stock_csv_default_threshold_matches_json(client, fake_db):
    for sku, qty in (("Q9", 9), ("Q10", 10), ("Q11", 11)):
        fake_db.add_item(
            sku=sku, name=sku, warehouse_id="w1", quantity=qty, tenant_id="tenant-a"
        )
    json_resp = client.get("/reports/low-stock", headers=TENANT_A)
    csv_resp = client.get("/reports/low-stock.csv", headers=TENANT_A)
    assert json_resp.status_code == csv_resp.status_code == 200
    json_skus = [i["sku"] for i in json_resp.json()["items"]]
    assert [r["sku"] for r in _csv_rows(csv_resp)] == json_skus
    assert json_skus == ["Q9", "Q10"]


def test_low_stock_csv_scoped_to_tenant(client, fake_db):
    fake_db.add_item(
        sku="A1", name="a1", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="A2", name="a2", warehouse_id="w2", quantity=3, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B1", name="b1", warehouse_id="w1", quantity=1, tenant_id="tenant-b"
    )
    fake_db.add_item(
        sku="B2", name="b2", warehouse_id="w2", quantity=2, tenant_id="tenant-b"
    )
    resp_a = client.get("/reports/low-stock.csv", headers=TENANT_A)
    assert resp_a.status_code == 200
    skus_a = [r["sku"] for r in _csv_rows(resp_a)]
    assert skus_a == ["A1", "A2"]
    assert "B1" not in resp_a.text and "B2" not in resp_a.text

    resp_b = client.get("/reports/low-stock.csv", headers={"X-Tenant-Id": "tenant-b"})
    assert resp_b.status_code == 200
    assert [r["sku"] for r in _csv_rows(resp_b)] == ["B1", "B2"]


def test_low_stock_csv_warehouse_filter(client, fake_db):
    fake_db.add_item(
        sku="W1A", name="w1a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="W2A", name="w2a", warehouse_id="w2", quantity=2, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="W1HI", name="w1hi", warehouse_id="w1", quantity=50, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="W1B", name="w1b", warehouse_id="w1", quantity=1, tenant_id="tenant-b"
    )
    resp = client.get(
        "/reports/low-stock.csv", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    rows = _csv_rows(resp)
    # Only w1, only tenant-a, and the threshold still applies.
    assert [r["sku"] for r in rows] == ["W1A"]
    assert all(r["warehouse_id"] == "w1" for r in rows)

    # Unknown warehouse: just the header row.
    resp = client.get(
        "/reports/low-stock.csv", params={"warehouse_id": "nope"}, headers=TENANT_A
    )
    assert resp.status_code == 200
    assert resp.text.splitlines() == ["sku,name,warehouse_id,quantity"]

    # No filter (or an empty one): every warehouse for the tenant.
    for params in ({}, {"warehouse_id": ""}):
        resp = client.get("/reports/low-stock.csv", params=params, headers=TENANT_A)
        assert [r["sku"] for r in _csv_rows(resp)] == ["W1A", "W2A"]


def test_low_stock_csv_missing_tenant_header_rejected_like_json(client, fake_db):
    json_resp = client.get("/reports/low-stock")
    csv_resp = client.get("/reports/low-stock.csv")
    assert json_resp.status_code in (400, 422)
    assert csv_resp.status_code == json_resp.status_code
    assert csv_resp.json() == json_resp.json()


@pytest.mark.parametrize("tenant", ["", "   "])
def test_low_stock_csv_invalid_tenant_header_matches_json(client, fake_db, tenant):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    headers = {"X-Tenant-Id": tenant}
    json_resp = client.get("/reports/low-stock", headers=headers)
    csv_resp = client.get("/reports/low-stock.csv", headers=headers)
    assert csv_resp.status_code == json_resp.status_code
    if json_resp.status_code == 200:
        # Accepted by both: neither may leak another tenant's rows.
        assert json_resp.json()["items"] == []
        assert _csv_rows(csv_resp) == []
    else:
        assert csv_resp.json() == json_resp.json()


def test_low_stock_csv_invalid_threshold_rejected_like_json(client, fake_db):
    params = {"threshold": "not-a-number"}
    json_resp = client.get("/reports/low-stock", params=params, headers=TENANT_A)
    csv_resp = client.get("/reports/low-stock.csv", params=params, headers=TENANT_A)
    assert json_resp.status_code == 422
    assert csv_resp.status_code == json_resp.status_code


def test_todays_movements_returns_recent_entries(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET",
        warehouse_id="w1",
        delta=-2,
        created_at=datetime.now(),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert any(m["sku"] == "WIDGET" for m in body["movements"])


def test_todays_movements_scoped_to_tenant(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET",
        warehouse_id="w1",
        delta=-2,
        created_at=datetime.now(),
        tenant_id="tenant-b",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["movements"] == []


def test_reserved_value_happy_path(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert line["sku"] == "WIDGET"
    assert line["reserved_qty"] == 3


def test_import_snapshot_happy_path(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.post(
        "/reports/import",
        json={"items": [{"sku": "WIDGET", "warehouse_id": "w1", "quantity": 50}]},
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == 1
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["quantity"] == 50


def test_import_snapshot_rejects_non_list_body(client, fake_db):
    resp = client.post("/reports/import", json={"items": "nope"}, headers=TENANT_A)
    assert resp.status_code == 400


def test_import_snapshot_rejects_malformed_entry(client, fake_db):
    resp = client.post(
        "/reports/import",
        json={"items": [{"sku": "WIDGET", "warehouse_id": "w1"}]},  # missing quantity
        headers=TENANT_A,
    )
    assert resp.status_code == 400
