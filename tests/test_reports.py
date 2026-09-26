from datetime import datetime

import pytest

from app import db as db_module
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


def test_low_stock_report_rejects_empty_tenant_header(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400
    # Same error body as the item-lookup routes.
    items_resp = client.get("/items/A", headers={"X-Tenant-Id": ""})
    assert items_resp.status_code == 400
    assert resp.json() == items_resp.json() == {"detail": "missing tenant"}


def test_low_stock_report_whitespace_tenant_header_matches_items_routes(
    client, fake_db
):
    # Whitespace-only handling must mirror app/routes/items.py exactly,
    # whatever that is.
    headers = {"X-Tenant-Id": "   "}
    resp = client.get("/reports/low-stock", headers=headers)
    items_resp = client.get("/items", headers=headers)
    assert resp.status_code == items_resp.status_code
    if items_resp.status_code == 400:
        assert resp.json() == items_resp.json()


def test_low_stock_report_valid_tenant_header_still_ok(client, fake_db):
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


TENANT_B = {"X-Tenant-Id": "tenant-b"}


def test_reserved_value_ignores_other_tenants_item_at_same_sku_and_warehouse(
    client, fake_db
):
    # Tenant B's item is seeded first so an unscoped join would match it first.
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


def test_reserved_value_other_tenant_excludes_foreign_reservations(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=100.0, tenant_id="tenant-b"
    )
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )

    resp = client.get("/reports/reserved-value", headers=TENANT_B)
    assert resp.status_code == 200
    assert resp.json()["lines"] == []


# -- import_snapshot atomicity ------------------------------------------------
#
# The shared FakeDB.transaction() has no rollback semantics, so these tests
# drive the *real* app.db.transaction() over a fake connection at the lowest
# layer (app.db.get_connection): writes are buffered and only applied to the
# FakeDB store on commit(), and discarded on rollback().


class _BufferingConnection:
    def __init__(self, fake_db, fail_on_write=None):
        self._fake_db = fake_db
        self._fail_on_write = fail_on_write
        self._pending: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, cursor_factory=None):
        return _BufferingCursor(self)

    def commit(self):
        for sql, params in self._pending:
            self._fake_db.execute(sql, params)
        self._pending.clear()
        self.commits += 1

    def rollback(self):
        self._pending.clear()
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _BufferingCursor:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def execute(self, sql, params=()):
        conn = self._conn
        if (
            conn._fail_on_write is not None
            and len(conn._pending) + 1 == conn._fail_on_write
        ):
            raise RuntimeError("simulated write failure")
        conn._pending.append((sql, params))
        self.rowcount = 1


@pytest.fixture
def txn_conns(fake_db, monkeypatch):
    """Route import_snapshot through the real app.db.transaction() backed by
    buffering fake connections. Returns (connections list, configure fn)."""
    conns: list[_BufferingConnection] = []
    opts = {"fail_on_write": None}

    def _get_connection():
        conn = _BufferingConnection(fake_db, fail_on_write=opts["fail_on_write"])
        conns.append(conn)
        return conn

    monkeypatch.setattr(db_module, "get_connection", _get_connection)
    monkeypatch.setattr(reports_routes, "transaction", db_module.transaction)
    # Any per-entry committing write would bypass the transaction; fail loudly.
    monkeypatch.setattr(
        reports_routes,
        "execute",
        lambda *a, **k: pytest.fail("import_snapshot must not use per-call execute()"),
        raising=False,
    )
    return conns, opts


def _seed_three(fake_db):
    for sku in ("A", "B", "C"):
        fake_db.add_item(sku=sku, warehouse_id="w1", quantity=1, tenant_id="tenant-a")


def _quantities(fake_db):
    return {r["sku"]: r["quantity"] for r in fake_db.items}


def test_import_snapshot_malformed_last_entry_writes_nothing(
    client, fake_db, txn_conns
):
    conns, _ = txn_conns
    _seed_three(fake_db)
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
    assert all(c.commits == 0 for c in conns)


def test_import_snapshot_malformed_middle_entry_writes_nothing(
    client, fake_db, txn_conns
):
    conns, _ = txn_conns
    _seed_three(fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": "not-a-number"},
                {"sku": "C", "warehouse_id": "w1", "quantity": 30},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "malformed snapshot entry"}
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}
    assert all(c.commits == 0 for c in conns)


def test_import_snapshot_write_failure_partway_rolls_back(client, fake_db, txn_conns):
    conns, opts = txn_conns
    opts["fail_on_write"] = 2
    _seed_three(fake_db)
    failing_client = type(client)(client.app, raise_server_exceptions=False)
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
    assert resp.status_code >= 500
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}
    assert len(conns) == 1
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].closed


def test_import_snapshot_valid_multi_entry_applies_all(client, fake_db, txn_conns):
    conns, _ = txn_conns
    _seed_three(fake_db)
    # Another tenant's row at the same SKU/warehouse must be untouched.
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
    a_rows = {
        r["sku"]: r["quantity"] for r in fake_db.items if r["tenant_id"] == "tenant-a"
    }
    assert a_rows == {"A": 10, "B": 20, "C": 30}
    b_row = next(r for r in fake_db.items if r["tenant_id"] == "tenant-b")
    assert b_row["quantity"] == 7
    assert len(conns) == 1
    assert conns[0].commits == 1
    assert conns[0].rollbacks == 0
