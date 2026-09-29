from datetime import datetime

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


def test_low_stock_report_empty_tenant_header_is_rejected(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing tenant"


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


def test_reserved_value_prices_with_own_tenant_item_only(client, fake_db):
    # Tenant B's item is seeded first so an unscoped join would pick it up.
    fake_db.add_item(
        sku="WIDGET",
        warehouse_id="w1",
        quantity=100,
        price=1000.0,
        tenant_id="tenant-b",
    )
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert len(lines) == 1
    assert lines[0]["reserved_qty"] == 3
    assert lines[0]["reserved_value"] == 6.0


def test_reserved_value_not_priced_from_other_tenant_item(client, fake_db):
    fake_db.add_item(
        sku="WIDGET",
        warehouse_id="w1",
        quantity=100,
        price=1000.0,
        tenant_id="tenant-b",
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json()["lines"] == []


def _seed_three(fake_db):
    for sku in ("A", "B", "C"):
        fake_db.add_item(sku=sku, warehouse_id="w1", quantity=1, tenant_id="tenant-a")


def _qty(fake_db, sku):
    return next(r["quantity"] for r in fake_db.items if r["sku"] == sku)


def test_import_snapshot_applies_every_entry(client, fake_db):
    _seed_three(fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": s, "warehouse_id": "w1", "quantity": q}
                for s, q in (("A", 10), ("B", 20), ("C", 30))
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == 3
    assert [_qty(fake_db, s) for s in "ABC"] == [10, 20, 30]


def test_import_snapshot_malformed_last_entry_writes_nothing(client, fake_db):
    _seed_three(fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": "not-a-number"},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "malformed snapshot entry"
    assert [_qty(fake_db, s) for s in "ABC"] == [1, 1, 1]


def test_import_snapshot_write_failure_rolls_back(client, fake_db, monkeypatch):
    from app.routes import reports as reports_routes

    _seed_three(fake_db)
    real_execute = fake_db.execute
    calls = {"n": 0}

    def failing_execute(sql, params=()):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated DB failure")
        return real_execute(sql, params)

    # Route the per-entry write through the failing fake at every layer.
    monkeypatch.setattr(fake_db, "execute", failing_execute)
    monkeypatch.setattr(reports_routes, "execute", failing_execute, raising=False)

    # Exercise the production app.db.transaction(): only its connection is
    # faked, so commit/rollback are driven by the real context manager.
    from app import db as db_module
    from conftest import _FakeConnection

    monkeypatch.setattr(db_module, "get_connection", lambda: _FakeConnection(fake_db))
    monkeypatch.setattr(reports_routes, "transaction", db_module.transaction)

    safe_client = type(client)(client.app, raise_server_exceptions=False)
    resp = safe_client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": s, "warehouse_id": "w1", "quantity": q}
                for s, q in (("A", 10), ("B", 20), ("C", 30))
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code >= 500
    assert [_qty(fake_db, s) for s in "ABC"] == [1, 1, 1]
