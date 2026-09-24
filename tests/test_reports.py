from datetime import datetime

import pytest

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


def test_low_stock_report_valid_tenant_header_returns_items(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=3, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", params={"threshold": 5}, headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json() == {
        "threshold": 5,
        "items": [{"sku": "A", "name": "a", "warehouse_id": "w1", "quantity": 3}],
    }


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


def test_reserved_value_prices_using_own_tenant_item(client, fake_db):
    # Tenant B's item shares SKU + warehouse with tenant A's and is seeded
    # first, so an unscoped join would pick it up.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=50.0, tenant_id="tenant-b"
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
    line = lines[0]
    assert line["sku"] == "WIDGET"
    assert line["warehouse_id"] == "w1"
    assert line["reserved_qty"] == 3
    assert line["reserved_value"] == 6.0


def test_reserved_value_ignores_other_tenants_item(client, fake_db):
    # Only tenant B has an item at this SKU + warehouse: tenant A's
    # reservation must not be priced using it.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=50.0, tenant_id="tenant-b"
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


def _seed_import_items(fake_db):
    for sku in ("A", "B", "C"):
        fake_db.add_item(sku=sku, warehouse_id="w1", quantity=1, tenant_id="tenant-a")


def _quantities(fake_db):
    return {r["sku"]: r["quantity"] for r in fake_db.items}


def test_import_snapshot_malformed_last_entry_writes_nothing(client, fake_db):
    _seed_import_items(fake_db)
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
    assert resp.json() == {"detail": "malformed snapshot entry"}
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}


class _StagingConnection:
    """Low-level fake connection: writes are staged and only reach the
    FakeDB on commit(); rollback() discards them. Raises on the Nth write."""

    def __init__(self, fake_db, fail_on):
        self.fake_db = fake_db
        self.fail_on = fail_on
        self.writes = 0
        self.staged = []
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        conn = self

        class _Cursor:
            rowcount = 0

            def execute(self, sql, params=()):
                conn.writes += 1
                if conn.writes == conn.fail_on:
                    raise RuntimeError("simulated write failure")
                conn.staged.append((sql, params))
                self.rowcount = 1

        return _Cursor()

    def commit(self):
        for sql, params in self.staged:
            self.fake_db.execute(sql, params)
        self.staged = []
        self.committed = True

    def rollback(self):
        self.staged = []
        self.rolled_back = True

    def close(self):
        pass


def test_import_snapshot_write_failure_midway_persists_nothing(
    client, fake_db, monkeypatch
):
    from app import db as db_module
    from app.routes import reports as reports_routes

    _seed_import_items(fake_db)
    conn = _StagingConnection(fake_db, fail_on=3)
    # Exercise the real transaction() helper over the staging connection.
    monkeypatch.setattr(db_module, "get_connection", lambda: conn)
    monkeypatch.setattr(
        reports_routes, "transaction", db_module.transaction, raising=False
    )

    # Also fail the Nth per-statement execute(), for any code path that
    # writes entries one call at a time outside a shared transaction.
    calls = {"n": 0}
    real_execute = fake_db.execute

    def failing_execute(sql, params=()):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated write failure")
        return real_execute(sql, params)

    monkeypatch.setattr(reports_routes, "execute", failing_execute, raising=False)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        client.post(
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
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}
    assert not conn.committed
    assert conn.rolled_back


def test_import_snapshot_valid_snapshot_applies_every_entry(client, fake_db):
    _seed_import_items(fake_db)
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=7, tenant_id="tenant-b")
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
    assert resp.json() == {"items": 3, "snapshot": '{"received": 3}'}
    by_key = {(r["sku"], r["tenant_id"]): r["quantity"] for r in fake_db.items}
    assert by_key == {
        ("A", "tenant-a"): 10,
        ("B", "tenant-a"): 20,
        ("C", "tenant-a"): 30,
        ("A", "tenant-b"): 7,
    }
