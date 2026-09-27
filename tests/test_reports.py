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
    # Another tenant's item at the same SKU + warehouse is seeded first, so a
    # tenant-unscoped join would pick it up.
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
    assert lines[0]["sku"] == "WIDGET"
    assert lines[0]["reserved_qty"] == 3
    assert lines[0]["reserved_value"] == 6.0


def test_reserved_value_ignores_other_tenant_only_item(client, fake_db):
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


def test_low_stock_report_rejects_empty_tenant_header(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400
    assert resp.json() == {"detail": "missing tenant"}


def test_low_stock_report_empty_tenant_matches_items_routes(client, fake_db):
    empty = {"X-Tenant-Id": ""}
    items_resp = client.get("/items/WIDGET", headers=empty)
    reports_resp = client.get("/reports/low-stock", headers=empty)
    assert reports_resp.status_code == items_resp.status_code == 400
    assert reports_resp.json() == items_resp.json()


def test_low_stock_report_empty_tenant_does_not_query_db(client, fake_db, monkeypatch):
    from app.routes import reports as reports_routes

    def _boom(*args, **kwargs):
        raise AssertionError("database must not be queried for an empty tenant")

    monkeypatch.setattr(reports_routes, "fetch_all", _boom)
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400


def test_low_stock_report_valid_tenant_still_returns_data(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=3, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", params={"threshold": 5}, headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json() == {
        "threshold": 5,
        "items": [{"sku": "A", "name": "a", "warehouse_id": "w1", "quantity": 3}],
    }


class _BufferedConnection:
    """Fake psycopg2 connection with real commit/rollback semantics.

    Writes are buffered and only applied to the FakeDB on ``commit()``;
    ``rollback()`` discards them. ``fail_on`` makes the Nth statement raise.
    """

    def __init__(self, fake_db, fail_on=None):
        self._fake_db = fake_db
        self._fail_on = fail_on
        self._pending = []
        self.statements = 0
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        conn = self

        class _Cursor:
            rowcount = 0

            def execute(self, sql, params=()):
                conn.statements += 1
                if conn._fail_on is not None and conn.statements == conn._fail_on:
                    raise RuntimeError("simulated write failure")
                conn._pending.append((sql, params))

        return _Cursor()

    def commit(self):
        for sql, params in self._pending:
            self._fake_db.execute(sql, params)
        self._pending = []
        self.committed = True

    def rollback(self):
        self._pending = []
        self.rolled_back = True

    def close(self):
        self.closed = True


def _use_real_transaction(monkeypatch, conn):
    from app import db as db_module
    from app.routes import reports as reports_routes

    monkeypatch.setattr(db_module, "get_connection", lambda: conn)
    monkeypatch.setattr(reports_routes, "transaction", db_module.transaction)


def _seed_three(fake_db):
    for sku in ("A", "B", "C"):
        fake_db.add_item(sku=sku, warehouse_id="w1", quantity=1, tenant_id="tenant-a")


def _quantities(fake_db):
    return {
        r["sku"]: r["quantity"] for r in fake_db.items if r["tenant_id"] == "tenant-a"
    }


def test_import_snapshot_applies_all_entries(client, fake_db, monkeypatch):
    _seed_three(fake_db)
    conn = _BufferedConnection(fake_db)
    _use_real_transaction(monkeypatch, conn)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": "30"},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": 3, "snapshot": '{"received": 3}'}
    assert _quantities(fake_db) == {"A": 10, "B": 20, "C": 30}
    assert conn.committed and conn.closed and not conn.rolled_back


def test_import_snapshot_malformed_last_entry_writes_nothing(
    client, fake_db, monkeypatch
):
    from app.routes import reports as reports_routes

    _seed_three(fake_db)

    def _no_transaction():
        raise AssertionError("no transaction should be opened for a malformed snapshot")

    monkeypatch.setattr(reports_routes, "transaction", _no_transaction)
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


def test_import_snapshot_write_failure_rolls_back_all(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    _seed_three(fake_db)
    conn = _BufferedConnection(fake_db, fail_on=2)
    _use_real_transaction(monkeypatch, conn)
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
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}
    assert conn.rolled_back and conn.closed and not conn.committed
    assert conn.statements == 2
