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


def test_utc_start_of_day_is_aware_utc_midnight():
    now = datetime(2024, 3, 10, 15, 42, 7, 123, tzinfo=timezone.utc)
    start = reports_routes.utc_start_of_day(now)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_uses_utc_date_for_negative_offset():
    # 20:00 at UTC-5 on Mar 10 is 01:00 UTC on Mar 11.
    now = datetime(2024, 3, 10, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
    start = reports_routes.utc_start_of_day(now)
    assert start == datetime(2024, 3, 11, tzinfo=timezone.utc)
    assert start.tzinfo == timezone.utc


def test_utc_start_of_day_uses_utc_date_for_positive_offset():
    # 08:00 at UTC+10 on Mar 11 is 22:00 UTC on Mar 10.
    now = datetime(2024, 3, 11, 8, 0, tzinfo=timezone(timedelta(hours=10)))
    start = reports_routes.utc_start_of_day(now)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.tzinfo == timezone.utc


def test_utc_start_of_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes.utc_start_of_day(datetime(2024, 3, 10, 12, 0))  # noqa: DTZ001


def _pin_clock(monkeypatch, now):
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: now)


def test_todays_movements_passes_aware_utc_midnight_to_query(client, monkeypatch):
    _pin_clock(monkeypatch, datetime(2024, 3, 11, 1, 0, tzinfo=timezone.utc))
    captured = {}

    def fake_fetch_all(sql, params=()):
        captured["params"] = params
        return []

    monkeypatch.setattr(reports_routes, "fetch_all", fake_fetch_all)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    tenant_id, cutoff = captured["params"]
    assert tenant_id == "tenant-a"
    assert cutoff == datetime(2024, 3, 11, tzinfo=timezone.utc)
    assert cutoff.utcoffset() == timedelta(0)
    assert resp.json()["date"] == "2024-03-11"


def test_todays_movements_filters_on_utc_calendar_day(client, fake_db, monkeypatch):
    _pin_clock(monkeypatch, datetime(2024, 3, 11, 1, 30, tzinfo=timezone.utc))
    fake_db.add_movement(
        sku="YESTERDAY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 10, 23, 59, 59, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="MIDNIGHT",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 11, 0, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="TODAY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 11, 1, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-11"
    assert [m["sku"] for m in body["movements"]] == ["MIDNIGHT", "TODAY"]


def test_todays_movements_ignores_server_local_timezone(client, fake_db, monkeypatch):
    # Simulate a server whose local zone is well behind UTC: local wall-clock
    # is still Mar 10, but the UTC date is already Mar 11.
    import time

    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset unavailable on this platform")
    monkeypatch.setenv("TZ", "Etc/GMT+8")  # POSIX sign: UTC-8
    time.tzset()
    try:
        _pin_clock(monkeypatch, datetime(2024, 3, 11, 2, 0, tzinfo=timezone.utc))
        fake_db.add_movement(
            sku="LOCAL_EVENING",
            warehouse_id="w1",
            delta=-1,
            created_at=datetime(2024, 3, 10, 20, 0, tzinfo=timezone.utc),
            tenant_id="tenant-a",
        )
        fake_db.add_movement(
            sku="UTC_TODAY",
            warehouse_id="w1",
            delta=-1,
            created_at=datetime(2024, 3, 11, 0, 30, tzinfo=timezone.utc),
            tenant_id="tenant-a",
        )
        resp = client.get("/reports/today", headers=TENANT_A)
    finally:
        monkeypatch.undo()
        time.tzset()
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-11"
    assert [m["sku"] for m in body["movements"]] == ["UTC_TODAY"]
