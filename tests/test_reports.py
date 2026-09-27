from datetime import datetime, timedelta, timezone

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


def test_utc_start_of_day_converts_aware_input_to_utc_date():
    from app.routes.reports import utc_start_of_day

    # 23:30 at UTC-5 on Jan 1 is 04:30 UTC on Jan 2.
    local = datetime(2024, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    result = utc_start_of_day(local)
    assert result == datetime(2024, 1, 2, tzinfo=timezone.utc)
    assert result.utcoffset() == timedelta(0)


def test_utc_start_of_day_default_is_current_utc_midnight():
    from app.routes.reports import utc_start_of_day

    result = utc_start_of_day()
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)
    assert result.date() == datetime.now(timezone.utc).date()
    assert (result.hour, result.minute, result.second, result.microsecond) == (0, 0, 0, 0)


def test_todays_movements_queries_with_aware_utc_boundary(client, monkeypatch):
    from app.routes import reports

    captured = {}

    def fake_fetch_all(sql, params=()):
        captured["params"] = params
        return []

    monkeypatch.setattr(reports, "fetch_all", fake_fetch_all)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    boundary = captured["params"][1]
    assert boundary.tzinfo is not None
    assert boundary.utcoffset() == timedelta(0)
    assert boundary == reports.utc_start_of_day()
    assert resp.json()["date"] == boundary.date().isoformat()
