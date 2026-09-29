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


def test_low_stock_report_rejects_empty_tenant_header(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400
    # Same error body the item-lookup routes return for an empty tenant header.
    items_resp = client.get("/items/A", headers={"X-Tenant-Id": ""})
    assert items_resp.status_code == 400
    assert resp.json() == items_resp.json() == {"detail": "missing tenant"}


def test_low_stock_report_valid_tenant_still_succeeds(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json()["threshold"] == 10
    assert [i["sku"] for i in resp.json()["items"]] == ["A"]


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


def test_reserved_value_uses_only_same_tenant_item(client, fake_db):
    # Tenant B's item is added first so an unscoped join would match it.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=50.0, tenant_id="tenant-b"
    )
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    fake_db.add_reservation(
        order_id="o2", tenant_id="tenant-b", sku="WIDGET", warehouse_id="w1", quantity=4
    )

    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert len(lines) == 1
    assert lines[0]["reserved_qty"] == 3
    assert lines[0]["reserved_value"] == 3 * 2.0

    resp = client.get("/reports/reserved-value", headers={"X-Tenant-Id": "tenant-b"})
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert len(lines) == 1
    assert lines[0]["reserved_qty"] == 4
    assert lines[0]["reserved_value"] == 4 * 50.0


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


def _seed_two(fake_db):
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=2, tenant_id="tenant-a")


def _qty(fake_db, sku):
    return next(r for r in fake_db.items if r["sku"] == sku)["quantity"]


def test_import_snapshot_malformed_last_entry_writes_nothing(client, fake_db):
    _seed_two(fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": "lots"},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "malformed snapshot entry"}
    assert _qty(fake_db, "A") == 1
    assert _qty(fake_db, "B") == 2


def test_import_snapshot_malformed_middle_entry_writes_nothing(client, fake_db):
    _seed_two(fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "X", "quantity": 5},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "malformed snapshot entry"}
    assert _qty(fake_db, "A") == 1
    assert _qty(fake_db, "B") == 2


def test_import_snapshot_write_failure_rolls_back_all(fake_db, monkeypatch):
    import copy
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import reports as reports_routes

    _seed_two(fake_db)
    real_execute = fake_db.execute
    calls = {"n": 0}

    def flaky_execute(sql, params=()):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated write failure")
        return real_execute(sql, params)

    monkeypatch.setattr(fake_db, "execute", flaky_execute)

    # A transaction fake with real rollback: restore the item rows on error.
    base_transaction = fake_db.transaction

    @contextmanager
    def rollback_transaction():
        saved = copy.deepcopy(fake_db.items)
        try:
            with base_transaction() as conn:
                yield conn
        except Exception:
            fake_db.items = saved
            raise

    monkeypatch.setattr(reports_routes, "transaction", rollback_transaction)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code >= 500
    assert calls["n"] == 2
    assert _qty(fake_db, "A") == 1
    assert _qty(fake_db, "B") == 2


def test_import_snapshot_valid_body_applies_every_entry(client, fake_db):
    _seed_two(fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": "20"},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": 2, "snapshot": '{"received": 2}'}
    assert _qty(fake_db, "A") == 10
    assert _qty(fake_db, "B") == 20
