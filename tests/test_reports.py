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


# --- /reports/today uses a tz-aware UTC midnight boundary ---------------------


def test_utc_start_of_day_returns_aware_utc_midnight():
    now = datetime(2024, 3, 11, 15, 42, 7, 123456, tzinfo=timezone.utc)
    start = reports_routes.utc_start_of_day(now)
    assert start == datetime(2024, 3, 11, tzinfo=timezone.utc)
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_uses_utc_calendar_date_not_local_offset():
    # 20:00 at UTC-5 on the 10th is already 01:00 UTC on the 11th.
    eastern = timezone(timedelta(hours=-5))
    now = datetime(2024, 3, 10, 20, 0, tzinfo=eastern)
    assert reports_routes.utc_start_of_day(now) == datetime(
        2024, 3, 11, tzinfo=timezone.utc
    )
    # 08:00 at UTC+10 on the 11th is still 22:00 UTC on the 10th.
    sydney = timezone(timedelta(hours=10))
    now = datetime(2024, 3, 11, 8, 0, tzinfo=sydney)
    assert reports_routes.utc_start_of_day(now) == datetime(
        2024, 3, 10, tzinfo=timezone.utc
    )


def test_utc_start_of_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes.utc_start_of_day(datetime(2024, 3, 11, 12, 0))  # noqa: DTZ001


def test_todays_movements_boundary_is_utc_midnight(client, fake_db, monkeypatch):
    fixed_now = datetime(2024, 3, 11, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: fixed_now)
    captured = {}
    real_fetch_all = fake_db.fetch_all

    def spy(sql, params=()):
        if "FROM movements" in sql:
            captured["params"] = params
        return real_fetch_all(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)

    utc = timezone.utc
    fake_db.add_movement(
        sku="YESTERDAY",
        warehouse_id="w1",
        delta=1,
        created_at=datetime(2024, 3, 10, 23, 59, 59, tzinfo=utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="MIDNIGHT",
        warehouse_id="w1",
        delta=1,
        created_at=datetime(2024, 3, 11, 0, 0, tzinfo=utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="EARLY",
        warehouse_id="w1",
        delta=1,
        created_at=datetime(2024, 3, 11, 0, 30, tzinfo=utc),
        tenant_id="tenant-a",
    )

    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-11"
    assert [m["sku"] for m in body["movements"]] == ["MIDNIGHT", "EARLY"]

    _, boundary = captured["params"]
    assert boundary.tzinfo is not None
    assert boundary.utcoffset() == timedelta(0)
    assert boundary == datetime(2024, 3, 11, tzinfo=utc)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires time.tzset")
@pytest.mark.parametrize(
    "tz_name", ["America/New_York", "Asia/Tokyo", "Pacific/Kiritimati"]
)
def test_todays_movements_ignores_server_local_timezone(
    client, fake_db, monkeypatch, tz_name
):
    monkeypatch.setenv("TZ", tz_name)
    time.tzset()
    try:
        captured = {}
        real_fetch_all = fake_db.fetch_all

        def spy(sql, params=()):
            if "FROM movements" in sql:
                captured["params"] = params
            return real_fetch_all(sql, params)

        monkeypatch.setattr(reports_routes, "fetch_all", spy)
        before = datetime.now(timezone.utc)
        resp = client.get("/reports/today", headers=TENANT_A)
        after = datetime.now(timezone.utc)
        assert resp.status_code == 200
        _, boundary = captured["params"]
        assert boundary.utcoffset() == timedelta(0)
        expected = {
            datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            for d in (before, after)
        }
        assert boundary in expected
        assert resp.json()["date"] == boundary.date().isoformat()
    finally:
        monkeypatch.undo()
        time.tzset()
