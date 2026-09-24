from datetime import datetime, timedelta, timezone

import pytest

from app.routes import reports as reports_routes

TENANT_A = {"X-Tenant-Id": "tenant-a"}


def test_low_stock_report_happy_path(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
    fake_db.add_item(sku="B", name="b", warehouse_id="w1", quantity=99, tenant_id="tenant-a")
    resp = client.get("/reports/low-stock", params={"threshold": 10}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_low_stock_report_scoped_to_tenant(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", name="b", warehouse_id="w1", quantity=1, tenant_id="tenant-b")
    resp = client.get("/reports/low-stock", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_todays_movements_returns_recent_entries(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET", warehouse_id="w1", delta=-2, created_at=datetime.now(timezone.utc), tenant_id="tenant-a"
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert any(m["sku"] == "WIDGET" for m in body["movements"])


def test_todays_movements_scoped_to_tenant(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET", warehouse_id="w1", delta=-2, created_at=datetime.now(timezone.utc), tenant_id="tenant-b"
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["movements"] == []


def test_reserved_value_happy_path(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a")
    fake_db.add_reservation(order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3)
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


def test_utc_start_of_day_is_aware_utc_midnight():
    now = datetime(2024, 3, 10, 15, 42, 7, 123456, tzinfo=timezone.utc)
    start = reports_routes.utc_start_of_day(now)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.tzinfo is not None
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_converts_to_utc_before_truncating():
    # 20:30 on Mar 10 at UTC-05:00 is 01:30 on Mar 11 UTC.
    minus_five = timezone(timedelta(hours=-5))
    now = datetime(2024, 3, 10, 20, 30, tzinfo=minus_five)
    assert reports_routes.utc_start_of_day(now) == datetime(2024, 3, 11, tzinfo=timezone.utc)

    # 03:00 on Mar 11 at UTC+09:00 is 18:00 on Mar 10 UTC.
    plus_nine = timezone(timedelta(hours=9))
    now = datetime(2024, 3, 11, 3, 0, tzinfo=plus_nine)
    assert reports_routes.utc_start_of_day(now) == datetime(2024, 3, 10, tzinfo=timezone.utc)


def test_utc_start_of_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes.utc_start_of_day(datetime(2024, 3, 10, 12, 0))  # noqa: DTZ001


def test_todays_movements_uses_utc_midnight_boundary(client, fake_db, monkeypatch):
    now = datetime(2024, 3, 10, 1, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utc_now", lambda: now)
    fake_db.add_movement(
        sku="BEFORE",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 9, 23, 59, 59, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="AT_MIDNIGHT",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 10, 0, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="AFTER",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 10, 1, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-10"
    assert [m["sku"] for m in body["movements"]] == ["AT_MIDNIGHT", "AFTER"]


def test_todays_movements_passes_aware_utc_cutoff_to_query(client, fake_db, monkeypatch):
    now = datetime(2024, 3, 10, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utc_now", lambda: now)
    captured = {}

    def spy(sql, params=()):
        captured["params"] = params
        return fake_db.fetch_all(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    _, cutoff = captured["params"]
    assert cutoff == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert cutoff.tzinfo is not None
    assert cutoff.utcoffset() == timedelta(0)


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="requires time.tzset")
@pytest.mark.parametrize("tz", ["America/Los_Angeles", "Asia/Tokyo", "UTC"])
def test_todays_movements_independent_of_server_timezone(client, fake_db, monkeypatch, tz):
    import time

    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        before = datetime.now(timezone.utc)
        resp = client.get("/reports/today", headers=TENANT_A)
        after = datetime.now(timezone.utc)
    finally:
        monkeypatch.undo()
        time.tzset()
    assert resp.status_code == 200
    assert resp.json()["date"] in {before.date().isoformat(), after.date().isoformat()}
