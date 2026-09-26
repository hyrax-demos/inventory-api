import os
import time
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


# -- /reports/today UTC boundary --


def _capture_movement_params(fake_db, monkeypatch):
    captured = {}
    original = fake_db.fetch_all

    def spy(sql, params=()):
        if "FROM movements" in sql:
            captured["params"] = params
        return original(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)
    return captured


def test_utc_start_of_day_is_aware_utc_midnight():
    now = datetime(2024, 3, 10, 15, 42, 7, 123456, tzinfo=timezone.utc)
    start = reports_routes._utc_start_of_day(now)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_converts_other_offsets_to_utc_date():
    # 2024-03-10 22:30 at UTC-05:00 is 2024-03-11 03:30 UTC.
    now = datetime(2024, 3, 10, 22, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert reports_routes._utc_start_of_day(now) == datetime(2024, 3, 11, tzinfo=timezone.utc)
    # 2024-03-11 01:30 at UTC+09:00 is 2024-03-10 16:30 UTC.
    now = datetime(2024, 3, 11, 1, 30, tzinfo=timezone(timedelta(hours=9)))
    assert reports_routes._utc_start_of_day(now) == datetime(2024, 3, 10, tzinfo=timezone.utc)


def test_utc_start_of_day_rejects_naive():
    with pytest.raises(ValueError):
        reports_routes._utc_start_of_day(datetime(2024, 3, 10, 12, 0))  # noqa: DTZ001


def test_todays_movements_passes_aware_utc_midnight(client, fake_db, monkeypatch):
    fixed_now = datetime(2024, 3, 10, 2, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: fixed_now)
    captured = _capture_movement_params(fake_db, monkeypatch)

    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json()["date"] == "2024-03-10"
    _, cutoff = captured["params"]
    assert cutoff.tzinfo is not None
    assert cutoff == datetime(2024, 3, 10, tzinfo=timezone.utc)


def test_todays_movements_uses_utc_day_boundary(client, fake_db, monkeypatch):
    fixed_now = datetime(2024, 3, 10, 2, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: fixed_now)
    utc = timezone.utc
    fake_db.add_movement(
        sku="YESTERDAY", warehouse_id="w1", delta=1,
        created_at=datetime(2024, 3, 9, 23, 59, 59, tzinfo=utc), tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="MIDNIGHT", warehouse_id="w1", delta=1,
        created_at=datetime(2024, 3, 10, 0, 0, tzinfo=utc), tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="TODAY", warehouse_id="w1", delta=1,
        created_at=datetime(2024, 3, 10, 1, 0, tzinfo=utc), tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    assert [m["sku"] for m in resp.json()["movements"]] == ["MIDNIGHT", "TODAY"]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires time.tzset")
@pytest.mark.parametrize("tz", ["America/New_York", "Asia/Tokyo", "Pacific/Kiritimati"])
def test_todays_movements_cutoff_independent_of_server_timezone(client, fake_db, monkeypatch, tz):
    captured = _capture_movement_params(fake_db, monkeypatch)
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        before = datetime.now(timezone.utc)
        resp = client.get("/reports/today", headers=TENANT_A)
        after = datetime.now(timezone.utc)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    assert resp.status_code == 200
    _, cutoff = captured["params"]
    assert cutoff.utcoffset() == timedelta(0)
    expected = {
        datetime(d.year, d.month, d.day, tzinfo=timezone.utc) for d in (before, after)
    }
    assert cutoff in expected
    assert resp.json()["date"] == cutoff.date().isoformat()
