from datetime import datetime, timedelta, timezone

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


def test_utc_start_of_day_normalizes_non_utc_aware_input():
    # 2024-03-10 01:30 at UTC+05:00 is 2024-03-09 20:30 UTC.
    tz = timezone(timedelta(hours=5))
    start = reports_routes.utc_start_of_day(datetime(2024, 3, 10, 1, 30, tzinfo=tz))
    assert start == datetime(2024, 3, 9, tzinfo=timezone.utc)
    assert start.tzinfo is not None and start.utcoffset() == timedelta(0)


def test_utc_start_of_day_default_is_aware_utc_midnight():
    start = reports_routes.utc_start_of_day()
    now = datetime.now(timezone.utc)
    assert start.utcoffset() == timedelta(0)
    assert (
        start.date() == now.date()
        or start.date() == (now - timedelta(minutes=1)).date()
    )
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)


def test_todays_movements_passes_aware_utc_boundary(client, fake_db, monkeypatch):
    captured = {}

    def fake_fetch_all(sql, params=()):
        captured["params"] = params
        return []

    monkeypatch.setattr(reports_routes, "fetch_all", fake_fetch_all)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    boundary = captured["params"][1]
    assert boundary.tzinfo is not None and boundary.utcoffset() == timedelta(0)
    assert resp.json()["date"] == boundary.date().isoformat()


def test_todays_movements_excludes_yesterday_utc(client, fake_db):
    now = datetime.now(timezone.utc)
    fake_db.add_movement(
        sku="OLD",
        warehouse_id="w1",
        delta=1,
        created_at=reports_routes.utc_start_of_day(now) - timedelta(seconds=1),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="NEW", warehouse_id="w1", delta=1, created_at=now, tenant_id="tenant-a"
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    skus = [m["sku"] for m in resp.json()["movements"]]
    assert "OLD" not in skus and "NEW" in skus
