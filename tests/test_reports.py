from datetime import datetime, timedelta, timezone

import pytest

from app.routes import reports as reports_routes

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


def test_todays_movements_returns_recent_entries(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET",
        warehouse_id="w1",
        delta=-2,
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
        tenant_id="tenant-b",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["movements"] == []


def test_todays_movements_bound_is_utc_midnight(client, fake_db, monkeypatch):
    # 23:30 at UTC-5 on Jan 1 is 04:30 UTC on Jan 2: the report must start at
    # UTC midnight of Jan 2, not local midnight of Jan 1.
    frozen = datetime(2024, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: frozen)

    captured = []
    real_fetch_all = fake_db.fetch_all

    def spy(sql, params=()):
        captured.append(params)
        return real_fetch_all(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)

    fake_db.add_movement(
        sku="BEFORE",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 1, 1, 23, 59, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="AFTER",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 1, 2, 0, 1, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )

    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200

    (params,) = captured
    bound = params[1]
    assert bound.tzinfo is not None
    assert bound.utcoffset() == timedelta(0)
    assert bound == datetime(2024, 1, 2, tzinfo=timezone.utc)

    body = resp.json()
    assert body["date"] == "2024-01-02"
    assert [m["sku"] for m in body["movements"]] == ["AFTER"]


def test_start_of_utc_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes._start_of_utc_day(datetime(2024, 1, 1, 12, 0))  # noqa: DTZ001


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


def test_reserved_value_uses_own_tenant_item_price(client, fake_db):
    # Two tenants stock the same SKU in the same warehouse at different prices.
    # The other tenant's row is seeded first so an unscoped join would pick it.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=50.0, tenant_id="tenant-b"
    )
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    fake_db.add_reservation(
        order_id="o2", tenant_id="tenant-b", sku="WIDGET", warehouse_id="w1", quantity=7
    )

    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    (line,) = resp.json()["lines"]
    assert line["sku"] == "WIDGET"
    assert line["reserved_qty"] == 3
    assert line["reserved_value"] == pytest.approx(6.0)

    resp_b = client.get("/reports/reserved-value", headers={"X-Tenant-Id": "tenant-b"})
    assert resp_b.status_code == 200
    (line_b,) = resp_b.json()["lines"]
    assert line_b["reserved_qty"] == 7
    assert line_b["reserved_value"] == pytest.approx(350.0)


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


def test_low_stock_report_empty_tenant_header_is_rejected(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400


def test_import_snapshot_empty_tenant_header_is_rejected(client, fake_db):
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.post(
        "/reports/import",
        json={"items": [{"sku": "A", "warehouse_id": "w1", "quantity": 50}]},
        headers={"X-Tenant-Id": ""},
    )
    assert resp.status_code == 400
    assert fake_db.items[0]["quantity"] == 1
