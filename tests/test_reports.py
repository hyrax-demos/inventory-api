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
    assert resp.json() == {"detail": "missing tenant"}


def test_low_stock_report_valid_tenant_header_still_returns_report(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=3, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", params={"threshold": 5}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["threshold"] == 5
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


def test_reserved_value_prices_with_same_tenant_item_only(client, fake_db):
    # Tenant B's item row is seeded first so a join that ignores tenant would
    # match it for tenant A's reservation.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=100.0, tenant_id="tenant-b"
    )
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    fake_db.add_reservation(
        order_id="o2", tenant_id="tenant-b", sku="WIDGET", warehouse_id="w1", quantity=5
    )

    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert len(lines) == 1
    assert lines[0]["sku"] == "WIDGET"
    assert lines[0]["reserved_qty"] == 3
    assert lines[0]["reserved_value"] == 3 * 2.0

    resp_b = client.get("/reports/reserved-value", headers={"X-Tenant-Id": "tenant-b"})
    assert resp_b.status_code == 200
    lines_b = resp_b.json()["lines"]
    assert len(lines_b) == 1
    assert lines_b[0]["reserved_qty"] == 5
    assert lines_b[0]["reserved_value"] == 5 * 100.0


def test_reserved_value_ignores_other_tenants_item_when_own_missing(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=100.0, tenant_id="tenant-b"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json()["lines"] == []


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


def _install_rollback_transaction(monkeypatch, fake_db):
    """Replace the route's transaction() with one that restores the item
    store if the block raises, mirroring app.db.transaction's rollback."""
    import copy
    from contextlib import contextmanager

    from app.routes import reports as reports_routes

    real = fake_db.transaction

    @contextmanager
    def rolling_back_transaction():
        saved = copy.deepcopy(fake_db.items)
        try:
            with real() as conn:
                yield conn
        except Exception:
            fake_db.items = saved
            raise

    monkeypatch.setattr(reports_routes, "transaction", rolling_back_transaction)


def _quantities(fake_db):
    return {r["sku"]: r["quantity"] for r in fake_db.items}


def test_import_snapshot_malformed_last_entry_writes_nothing(
    client, fake_db, monkeypatch
):
    _install_rollback_transaction(monkeypatch, fake_db)
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
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
    assert _quantities(fake_db) == {"A": 1, "B": 2}


def test_import_snapshot_write_failure_mid_list_rolls_back(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import reports as reports_routes

    _install_rollback_transaction(monkeypatch, fake_db)
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
    fake_db.add_item(sku="C", warehouse_id="w1", quantity=3, tenant_id="tenant-a")

    real_apply = reports_routes._apply_stock_update
    calls = {"n": 0}

    def flaky_apply(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated write failure")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(reports_routes, "_apply_stock_update", flaky_apply)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": 30},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 500
    assert calls["n"] == 2
    assert _quantities(fake_db) == {"A": 1, "B": 2, "C": 3}


def test_import_snapshot_valid_snapshot_applies_all_entries(
    client, fake_db, monkeypatch
):
    _install_rollback_transaction(monkeypatch, fake_db)
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
    fake_db.add_item(sku="C", warehouse_id="w1", quantity=3, tenant_id="tenant-a")
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": 30},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == 3
    assert _quantities(fake_db) == {"A": 10, "B": 20, "C": 30}
