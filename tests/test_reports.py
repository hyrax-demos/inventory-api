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
    # A tenant-less item would match an unscoped "tenant_id = ''" query.
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


def test_reserved_value_prices_only_with_same_tenant_item(client, fake_db):
    # Tenant B's item is seeded first so a tenant-blind join would match it.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=100.0, tenant_id="tenant-b"
    )
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    # Exactly one line: not duplicated by a cross-tenant item match.
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert line["sku"] == "WIDGET"
    assert line["warehouse_id"] == "w1"
    assert line["reserved_qty"] == 3
    assert line["reserved_value"] == 3 * 2.0


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


# -- atomic snapshot import --------------------------------------------------


class _StagingConnection:
    """A fake psycopg2 connection that buffers writes until ``commit()``.

    Installed underneath the real ``app.db.transaction()`` so the route's
    transaction boundary (commit on success, rollback on exception) is what
    decides whether the staged UPDATEs reach the FakeDB store.
    """

    def __init__(self, fake_db, fail_on_nth_write=None):
        self._fake_db = fake_db
        self._fail_on = fail_on_nth_write
        self._writes = 0
        self._staged = []
        self.committed = False
        self.rolled_back = False

    def cursor(self, cursor_factory=None):
        conn = self

        class _Cursor:
            rowcount = 0

            def execute(self, sql, params=()):
                conn._writes += 1
                if conn._fail_on is not None and conn._writes == conn._fail_on:
                    raise RuntimeError("simulated write failure")
                conn._staged.append((sql, params))

        return _Cursor()

    def commit(self):
        for sql, params in self._staged:
            self._fake_db.execute(sql, params)
        self._staged = []
        self.committed = True

    def rollback(self):
        self._staged = []
        self.rolled_back = True

    def close(self):
        pass


def _use_staging_connection(monkeypatch, fake_db, **kw):
    from app import db
    from app.routes import reports as reports_routes

    conn = _StagingConnection(fake_db, **kw)
    monkeypatch.setattr(db, "get_connection", lambda: conn)
    monkeypatch.setattr(reports_routes, "transaction", db.transaction)
    return conn


def _seed_three(fake_db):
    for sku in ("A", "B", "C"):
        fake_db.add_item(sku=sku, warehouse_id="w1", quantity=1, tenant_id="tenant-a")


def _quantities(fake_db):
    return {r["sku"]: r["quantity"] for r in fake_db.items}


def test_import_snapshot_malformed_last_entry_writes_nothing(
    client, fake_db, monkeypatch
):
    _seed_three(fake_db)
    conn = _use_staging_connection(monkeypatch, fake_db)
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
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}
    assert conn._writes == 0


def test_import_snapshot_write_failure_rolls_back_all(client, fake_db, monkeypatch):
    _seed_three(fake_db)
    conn = _use_staging_connection(monkeypatch, fake_db, fail_on_nth_write=3)
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
    assert conn.rolled_back and not conn.committed
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}


def test_import_snapshot_applies_every_entry(client, fake_db, monkeypatch):
    _seed_three(fake_db)
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=7, tenant_id="tenant-b")
    conn = _use_staging_connection(monkeypatch, fake_db)
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
    assert conn.committed
    by_tenant = {(r["tenant_id"], r["sku"]): r["quantity"] for r in fake_db.items}
    assert by_tenant == {
        ("tenant-a", "A"): 10,
        ("tenant-a", "B"): 20,
        ("tenant-a", "C"): 30,
        ("tenant-b", "A"): 7,  # other tenant untouched
    }
