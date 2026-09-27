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


def _freeze_now(monkeypatch, frozen_utc):
    """Pin ``datetime.now`` inside the reports module to ``frozen_utc``."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                # Simulate a server whose local clock is UTC-5.
                return (frozen_utc - timedelta(hours=5)).replace(tzinfo=None)
            return frozen_utc.astimezone(tz)

    monkeypatch.setattr(reports_routes, "datetime", _FrozenDatetime)


def test_utc_start_of_day_is_aware_utc_midnight():
    start = reports_routes.utc_start_of_day(
        datetime(2024, 3, 10, 15, 42, 7, 123, tzinfo=timezone.utc)
    )
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_uses_utc_calendar_date_not_local():
    # 22:30 at UTC-5 on Mar 10 is 03:30 UTC on Mar 11.
    local = datetime(2024, 3, 10, 22, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert reports_routes.utc_start_of_day(local) == datetime(
        2024, 3, 11, tzinfo=timezone.utc
    )
    # 06:00 at UTC+9 on Mar 11 is 21:00 UTC on Mar 10.
    local = datetime(2024, 3, 11, 6, 0, tzinfo=timezone(timedelta(hours=9)))
    assert reports_routes.utc_start_of_day(local) == datetime(
        2024, 3, 10, tzinfo=timezone.utc
    )


def test_utc_start_of_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes.utc_start_of_day(datetime(2024, 3, 10, 12, 0))  # noqa: DTZ001


def test_utc_start_of_day_defaults_to_current_utc_date():
    start = reports_routes.utc_start_of_day()
    assert start.tzinfo is not None and start.utcoffset() == timedelta(0)
    assert start.date() == datetime.now(timezone.utc).date()


def test_todays_movements_uses_utc_midnight_boundary(client, fake_db, monkeypatch):
    # Now: 03:30 UTC on Mar 11 (22:30 local on Mar 10 for a UTC-5 server).
    _freeze_now(monkeypatch, datetime(2024, 3, 11, 3, 30, tzinfo=timezone.utc))
    fake_db.add_movement(
        sku="YESTERDAY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 10, 23, 59, 59, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="TODAY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 11, 0, 0, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-11"
    assert [m["sku"] for m in body["movements"]] == ["TODAY"]


def test_todays_movements_passes_aware_utc_cutoff_to_query(
    client, fake_db, monkeypatch
):
    # Now: 21:00 UTC on Mar 10 (would be Mar 11 local for a UTC+ server).
    _freeze_now(monkeypatch, datetime(2024, 3, 10, 21, 0, tzinfo=timezone.utc))
    captured = {}

    def _capture(sql, params=()):
        captured["params"] = params
        return []

    monkeypatch.setattr(reports_routes, "fetch_all", _capture)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    _, cutoff = captured["params"]
    assert cutoff.tzinfo is not None
    assert cutoff.utcoffset() == timedelta(0)
    assert cutoff == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert resp.json()["date"] == "2024-03-10"
