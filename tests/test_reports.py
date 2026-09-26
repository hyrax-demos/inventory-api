import os
import time
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


# -- /reports/today: UTC day boundary --------------------------------------


def _freeze_utcnow(monkeypatch, frozen: datetime) -> None:
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: frozen)


def test_utc_start_of_day_is_aware_utc_midnight():
    now = datetime(2024, 3, 10, 17, 45, 12, 999, tzinfo=timezone.utc)
    start = reports_routes.utc_start_of_day(now)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.tzinfo is not None
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_converts_non_utc_input_to_utc_date():
    # 2024-03-10 20:00 at UTC-05:00 is 2024-03-11 01:00 UTC.
    now = datetime(2024, 3, 10, 20, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert reports_routes.utc_start_of_day(now) == datetime(
        2024, 3, 11, tzinfo=timezone.utc
    )


def test_utc_start_of_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes.utc_start_of_day(datetime(2024, 3, 10, 12, 0))  # noqa: DTZ001


def test_utcnow_is_timezone_aware_utc():
    now = reports_routes._utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_todays_movements_query_boundary_is_aware_utc_midnight(client, monkeypatch):
    _freeze_utcnow(monkeypatch, datetime(2024, 3, 11, 1, 30, tzinfo=timezone.utc))
    captured = {}

    def fake_fetch_all(sql, params=()):
        captured["params"] = params
        return []

    monkeypatch.setattr(reports_routes, "fetch_all", fake_fetch_all)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json()["date"] == "2024-03-11"
    tenant_id, cutoff = captured["params"]
    assert tenant_id == "tenant-a"
    assert cutoff == datetime(2024, 3, 11, tzinfo=timezone.utc)
    assert cutoff.tzinfo is not None
    assert cutoff.utcoffset() == timedelta(0)


def test_todays_movements_filters_on_utc_calendar_date(client, fake_db, monkeypatch):
    _freeze_utcnow(monkeypatch, datetime(2024, 3, 11, 1, 30, tzinfo=timezone.utc))
    # Just before UTC midnight: yesterday in UTC, must be excluded.
    fake_db.add_movement(
        sku="YESTERDAY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 10, 23, 59, 59, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    # Exactly UTC midnight and shortly after: today in UTC, must be included.
    fake_db.add_movement(
        sku="MIDNIGHT",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 11, 0, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="EARLY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 11, 0, 45, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-11"
    assert [m["sku"] for m in body["movements"]] == ["MIDNIGHT", "EARLY"]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset unavailable")
@pytest.mark.parametrize(
    "local_tz", ["America/New_York", "Asia/Tokyo", "Pacific/Kiritimati"]
)
def test_todays_movements_independent_of_server_local_timezone(
    client, fake_db, local_tz
):
    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = local_tz
    time.tzset()
    try:
        # Real clock, not frozen: the route must still pick UTC midnight of
        # the current UTC date regardless of the process's local timezone.
        now_utc = datetime.now(timezone.utc)
        utc_midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        fake_db.add_movement(
            sku="BEFORE",
            warehouse_id="w1",
            delta=-1,
            created_at=utc_midnight - timedelta(seconds=1),
            tenant_id="tenant-a",
        )
        fake_db.add_movement(
            sku="AFTER",
            warehouse_id="w1",
            delta=-1,
            created_at=utc_midnight,
            tenant_id="tenant-a",
        )
        resp = client.get("/reports/today", headers=TENANT_A)
        assert resp.status_code == 200
        body = resp.json()
        assert body["date"] == utc_midnight.date().isoformat()
        assert [m["sku"] for m in body["movements"]] == ["AFTER"]
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()
