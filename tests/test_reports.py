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


# -- UTC day boundary for /reports/today --


def test_utc_start_of_day_returns_aware_utc_midnight():
    now = datetime(2024, 3, 10, 15, 42, 7, 123456, tzinfo=timezone.utc)
    start = reports_routes.utc_start_of_day(now)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_uses_utc_calendar_date_not_local():
    # 20:00 on Mar 9 at UTC-8 is 04:00 on Mar 10 UTC -> UTC midnight of Mar 10.
    pacific = timezone(timedelta(hours=-8))
    start = reports_routes.utc_start_of_day(datetime(2024, 3, 9, 20, 0, tzinfo=pacific))
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.tzinfo == timezone.utc

    # 02:00 on Mar 10 at UTC+5 is 21:00 on Mar 9 UTC -> UTC midnight of Mar 9.
    plus5 = timezone(timedelta(hours=5))
    start = reports_routes.utc_start_of_day(datetime(2024, 3, 10, 2, 0, tzinfo=plus5))
    assert start == datetime(2024, 3, 9, tzinfo=timezone.utc)


def test_utc_start_of_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes.utc_start_of_day(datetime(2024, 3, 10, 12, 0))  # noqa: DTZ001


def test_todays_movements_passes_aware_utc_midnight_to_query(client, monkeypatch):
    fixed_now = datetime(2024, 3, 10, 3, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: fixed_now)
    captured = {}

    def fake_fetch_all(sql, params=()):
        captured["params"] = params
        return []

    monkeypatch.setattr(reports_routes, "fetch_all", fake_fetch_all)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    tenant_id, cutoff = captured["params"]
    assert tenant_id == "tenant-a"
    assert cutoff == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert cutoff.utcoffset() == timedelta(0)
    assert resp.json()["date"] == "2024-03-10"


def test_todays_movements_filters_on_utc_day_boundary(client, fake_db, monkeypatch):
    fixed_now = datetime(2024, 3, 10, 3, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: fixed_now)
    # created_at is stored as UTC.
    fake_db.add_movement(
        sku="YESTERDAY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 9, 23, 59, 59, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="MIDNIGHT",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 10, 0, 0, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="TODAY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 10, 2, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-10"
    assert [m["sku"] for m in body["movements"]] == ["MIDNIGHT", "TODAY"]
