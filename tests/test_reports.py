from datetime import datetime, timezone

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


def test_utc_start_of_day_returns_aware_utc_midnight():
    from datetime import timedelta, timezone

    from app.routes.reports import utc_start_of_day

    # 23:30 at UTC-05:00 on Jan 1 is 04:30 UTC on Jan 2.
    local = datetime(2024, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    result = utc_start_of_day(local)
    assert result == datetime(2024, 1, 2, tzinfo=timezone.utc)
    assert result.utcoffset() == timedelta(0)


def test_utc_start_of_day_rejects_naive_datetime():
    import pytest

    from app.routes.reports import utc_start_of_day

    with pytest.raises(ValueError):
        utc_start_of_day(datetime(2024, 1, 1, 12, 0))  # noqa: DTZ001 - naive on purpose


def test_todays_movements_uses_utc_midnight_boundary(client, fake_db, monkeypatch):
    from datetime import timezone

    from app.routes import reports as reports_routes

    fixed_now = datetime(2024, 3, 10, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utcnow", lambda: fixed_now)

    captured = {}
    original_fetch_all = reports_routes.fetch_all

    def spy(sql, params=()):
        captured["params"] = params
        return original_fetch_all(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)

    # created_at values are naive UTC, as stored in the database.
    def naive_utc(*args):
        return datetime(*args, tzinfo=timezone.utc).replace(tzinfo=None)

    fake_db.add_movement(
        sku="YESTERDAY", warehouse_id="w1", delta=-1,
        created_at=naive_utc(2024, 3, 9, 23, 59, 59), tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="MIDNIGHT", warehouse_id="w1", delta=-1,
        created_at=naive_utc(2024, 3, 10, 0, 0, 0), tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="TODAY", warehouse_id="w1", delta=-1,
        created_at=naive_utc(2024, 3, 10, 1, 30), tenant_id="tenant-a",
    )

    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-10"
    assert [m["sku"] for m in body["movements"]] == ["MIDNIGHT", "TODAY"]

    boundary = captured["params"][1]
    assert boundary.tzinfo is not None
    assert boundary == datetime(2024, 3, 10, tzinfo=timezone.utc)


def test_todays_movements_boundary_ignores_server_local_timezone(client, fake_db, monkeypatch):
    import os
    import time
    from datetime import timedelta, timezone

    import pytest

    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset unavailable on this platform")

    from app.routes import reports as reports_routes

    captured = {}
    original_fetch_all = reports_routes.fetch_all

    def spy(sql, params=()):
        captured["params"] = params
        return original_fetch_all(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14, far from UTC
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
    boundary = captured["params"][1]
    assert boundary.utcoffset() == timedelta(0)
    valid = {
        d.replace(hour=0, minute=0, second=0, microsecond=0) for d in (before, after)
    }
    assert boundary in valid
