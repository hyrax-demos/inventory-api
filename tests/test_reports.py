from datetime import datetime

from fastapi.testclient import TestClient

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
    # A row whose tenant_id is the empty string: if the empty header reached
    # the query, this row would be returned with a 200.
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400
    assert resp.json() == {"detail": "missing tenant"}


def test_low_stock_report_empty_tenant_error_matches_items(client, fake_db):
    empty = {"X-Tenant-Id": ""}
    items_resp = client.get("/items/A", headers=empty)
    report_resp = client.get("/reports/low-stock", headers=empty)
    assert items_resp.status_code == report_resp.status_code == 400
    assert report_resp.json() == items_resp.json()


def test_low_stock_report_valid_tenant_returns_expected_data(client, fake_db):
    fake_db.add_item(
        sku="LOW", name="low", warehouse_id="w1", quantity=3, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="LOWER", name="lower", warehouse_id="w2", quantity=0, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="HIGH", name="high", warehouse_id="w1", quantity=50, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", params={"threshold": 5}, headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json() == {
        "threshold": 5,
        "items": [
            {"sku": "LOWER", "name": "lower", "warehouse_id": "w2", "quantity": 0},
            {"sku": "LOW", "name": "low", "warehouse_id": "w1", "quantity": 3},
        ],
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


def test_reserved_value_prices_only_with_same_tenant_item(client, fake_db):
    # Both tenants stock the same SKU in the same warehouse at different prices.
    # Tenant B's row is seeded first so a tenant-blind join would pick it up.
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
    lines = resp.json()["lines"]
    assert len(lines) == 1
    line = lines[0]
    assert line["sku"] == "WIDGET"
    assert line["warehouse_id"] == "w1"
    assert line["reserved_qty"] == 3
    assert line["reserved_value"] == 3 * 2.0


def test_reserved_value_ignores_other_tenants_item(client, fake_db):
    # Only tenant B has an item row at this SKU/warehouse: tenant A's
    # reservation must not be priced with it.
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


class _StagedConnection:
    """A fake psycopg2 connection with real commit/rollback semantics.

    Writes are staged and only applied to the FakeDB on ``commit()``;
    ``rollback()`` discards them. ``fail_on`` makes the Nth ``execute`` call
    (1-based) raise, simulating a write failure mid-import.
    """

    def __init__(self, fake_db, fail_on=None):
        self._fake_db = fake_db
        self._fail_on = fail_on
        self._calls = 0
        self._pending: list = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self

    def execute(self, sql, params=()):
        self._calls += 1
        if self._fail_on is not None and self._calls == self._fail_on:
            raise RuntimeError("simulated write failure")
        self._pending.append((sql, params))

    @property
    def rowcount(self):
        return 1

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


def _use_staged_connection(monkeypatch, fake_db, fail_on=None):
    """Route import_snapshot through the real ``app.db.transaction`` helper,
    backed by a staged fake connection instead of Postgres."""
    from app import db as db_module
    from app.routes import reports as reports_routes

    conn = _StagedConnection(fake_db, fail_on=fail_on)
    connections = []

    def _get_connection():
        connections.append(conn)
        return conn

    monkeypatch.setattr(db_module, "get_connection", _get_connection)
    monkeypatch.setattr(reports_routes, "transaction", db_module.transaction)
    return conn, connections


def _seed_three(fake_db):
    for sku in ("A", "B", "C"):
        fake_db.add_item(sku=sku, warehouse_id="w1", quantity=1, tenant_id="tenant-a")


def _quantities(fake_db):
    return {r["sku"]: r["quantity"] for r in fake_db.items}


def test_import_snapshot_malformed_last_entry_writes_nothing(
    client, fake_db, monkeypatch
):
    _seed_three(fake_db)
    _, connections = _use_staged_connection(monkeypatch, fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1"},  # missing quantity
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "malformed snapshot entry"}
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}
    # Validation happens up front: no connection is even opened.
    assert connections == []


def test_import_snapshot_rejects_wrongly_typed_entry_and_writes_nothing(
    client, fake_db, monkeypatch
):
    _seed_three(fake_db)
    _, connections = _use_staged_connection(monkeypatch, fake_db)
    for bad in (
        {"sku": "C", "warehouse_id": "w1", "quantity": "lots"},
        {"sku": 7, "warehouse_id": "w1", "quantity": 3},
        {"sku": "C", "warehouse_id": None, "quantity": 3},
        "not-an-object",
    ):
        resp = client.post(
            "/reports/import",
            json={"items": [{"sku": "A", "warehouse_id": "w1", "quantity": 10}, bad]},
            headers=TENANT_A,
        )
        assert resp.status_code == 400, bad
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}
    assert connections == []


def test_import_snapshot_write_failure_rolls_back_all_entries(
    client, fake_db, monkeypatch
):
    _seed_three(fake_db)
    conn, connections = _use_staged_connection(monkeypatch, fake_db, fail_on=3)
    failing_client = TestClient(client.app, raise_server_exceptions=False)
    resp = failing_client.post(
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
    assert conn.rolled_back and not conn.committed and conn.closed
    # All writes shared the one connection.
    assert len(connections) == 1


def test_import_snapshot_applies_every_entry_in_one_transaction(
    client, fake_db, monkeypatch
):
    _seed_three(fake_db)
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-b")
    conn, connections = _use_staged_connection(monkeypatch, fake_db)
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
    tenant_a = {
        r["sku"]: r["quantity"] for r in fake_db.items if r["tenant_id"] == "tenant-a"
    }
    assert tenant_a == {"A": 10, "B": 20, "C": 30}
    # The other tenant's row at the same SKU/warehouse is untouched.
    tenant_b = [r for r in fake_db.items if r["tenant_id"] == "tenant-b"]
    assert tenant_b[0]["quantity"] == 1
    assert conn.committed and not conn.rolled_back and conn.closed
    assert len(connections) == 1
