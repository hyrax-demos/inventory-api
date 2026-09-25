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


def test_utc_start_of_day_is_aware_utc_midnight():
    now = datetime(2024, 3, 10, 15, 42, 7, 123456, tzinfo=timezone.utc)
    start = reports_routes.utc_start_of_day(now)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_uses_utc_calendar_date_not_local():
    # 20:00 on Mar 9 in UTC-5 is 01:00 on Mar 10 UTC -> UTC date is Mar 10.
    behind = datetime(2024, 3, 9, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert reports_routes.utc_start_of_day(behind) == datetime(
        2024, 3, 10, tzinfo=timezone.utc
    )
    # 03:00 on Mar 10 in UTC+9 is 18:00 on Mar 9 UTC -> UTC date is Mar 9.
    ahead = datetime(2024, 3, 10, 3, 0, tzinfo=timezone(timedelta(hours=9)))
    assert reports_routes.utc_start_of_day(ahead) == datetime(
        2024, 3, 9, tzinfo=timezone.utc
    )


def test_utc_start_of_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes.utc_start_of_day(datetime(2024, 3, 10, 12, 0))  # noqa: DTZ001


def test_todays_movements_passes_aware_utc_midnight_to_query(
    client, fake_db, monkeypatch
):
    fixed_now = datetime(2024, 3, 10, 1, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: fixed_now)
    captured = {}
    original = fake_db.fetch_all

    def spy(sql, params=()):
        if "FROM movements" in sql:
            captured["params"] = params
        return original(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    _, boundary = captured["params"]
    assert boundary == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert boundary.tzinfo is not None and boundary.utcoffset() == timedelta(0)
    assert resp.json()["date"] == "2024-03-10"


def test_todays_movements_uses_utc_day_boundary(client, fake_db, monkeypatch):
    # 01:30 UTC on Mar 10. A server in UTC-5 would think it is still Mar 9
    # locally; the report must still cut off at 2024-03-10T00:00Z.
    fixed_now = datetime(2024, 3, 10, 1, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: fixed_now)
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
        created_at=datetime(2024, 3, 10, 0, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="TODAY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 10, 1, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-10"
    assert [m["sku"] for m in body["movements"]] == ["MIDNIGHT", "TODAY"]


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
