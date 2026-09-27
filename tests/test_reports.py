from datetime import datetime

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
        sku="WIDGET", warehouse_id="w1", delta=-2, created_at=datetime.now(), tenant_id="tenant-a"
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert any(m["sku"] == "WIDGET" for m in body["movements"])


def test_todays_movements_scoped_to_tenant(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET", warehouse_id="w1", delta=-2, created_at=datetime.now(), tenant_id="tenant-b"
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


def test_utc_start_of_day_is_aware_utc_midnight():
    from datetime import timedelta, timezone

    from app.routes.reports import _utc_start_of_day

    # 01:30 at UTC+05:00 is 20:30 UTC on the *previous* day.
    local = datetime(2024, 3, 10, 1, 30, tzinfo=timezone(timedelta(hours=5)))
    start = _utc_start_of_day(local)
    assert start == datetime(2024, 3, 9, 0, 0, tzinfo=timezone.utc)
    assert start.utcoffset() == timedelta(0)

    # 22:00 at UTC-05:00 is 03:00 UTC on the *next* day.
    local = datetime(2024, 3, 10, 22, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert _utc_start_of_day(local) == datetime(2024, 3, 11, tzinfo=timezone.utc)

    # Naive input is treated as UTC.
    assert _utc_start_of_day(datetime(2024, 3, 10, 23, 59)) == datetime(  # noqa: DTZ001
        2024, 3, 10, tzinfo=timezone.utc
    )


def test_todays_movements_uses_utc_midnight_boundary(client, fake_db, monkeypatch):
    from datetime import timezone

    from app.routes import reports as reports_routes

    fixed_now = datetime(2024, 3, 10, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: fixed_now)

    captured = {}
    original = fake_db.fetch_all

    def spy(sql, params=()):
        if "FROM movements" in sql:
            captured["params"] = params
        return original(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)

    fake_db.add_movement(
        sku="YESTERDAY", warehouse_id="w1", delta=1,
        created_at=datetime(2024, 3, 9, 23, 59, 59, tzinfo=timezone.utc), tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="MIDNIGHT", warehouse_id="w1", delta=1,
        created_at=datetime(2024, 3, 10, 0, 0, 0, tzinfo=timezone.utc), tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="TODAY", warehouse_id="w1", delta=1,
        created_at=datetime(2024, 3, 10, 1, 0, tzinfo=timezone.utc), tenant_id="tenant-a",
    )

    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-10"
    assert [m["sku"] for m in body["movements"]] == ["MIDNIGHT", "TODAY"]

    _, boundary = captured["params"]
    assert boundary.tzinfo is not None
    assert boundary == datetime(2024, 3, 10, tzinfo=timezone.utc)


def test_todays_movements_ignores_server_local_timezone(client, fake_db, monkeypatch):
    import os
    import time

    if not hasattr(time, "tzset"):
        import pytest

        pytest.skip("time.tzset not available on this platform")
    from datetime import timezone

    from app.routes import reports as reports_routes

    captured = {}
    original = fake_db.fetch_all

    def spy(sql, params=()):
        if "FROM movements" in sql:
            captured["params"] = params
        return original(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14
    time.tzset()
    try:
        resp = client.get("/reports/today", headers=TENANT_A)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    assert resp.status_code == 200
    _, boundary = captured["params"]
    expected = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    assert boundary.utcoffset().total_seconds() == 0
    assert boundary.hour == boundary.minute == 0
    # Allow for the test straddling UTC midnight.
    assert abs((boundary - expected).total_seconds()) in (0, 86400)
