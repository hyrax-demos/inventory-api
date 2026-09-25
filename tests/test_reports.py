import copy
from contextlib import contextmanager
from datetime import datetime

import pytest

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
    assert resp.json() == {"detail": "missing tenant"}


def test_low_stock_report_empty_tenant_matches_items_route(client, fake_db):
    items_resp = client.get("/items/A", headers={"X-Tenant-Id": ""})
    reports_resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert reports_resp.status_code == items_resp.status_code == 400
    assert reports_resp.json() == items_resp.json()


def test_low_stock_report_valid_tenant_still_returns_data(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=3, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", params={"threshold": 5}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["threshold"] == 5
    assert [i["sku"] for i in body["items"]] == ["B", "A"]


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


def _seed_colliding_items(fake_db):
    # Tenant B's row is seeded first so a join that is not tenant-scoped
    # would pick it up (wrong price) before tenant A's own row.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=50.0, tenant_id="tenant-b"
    )
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )


def test_reserved_value_prices_with_own_tenant_item(client, fake_db):
    _seed_colliding_items(fake_db)
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert len(lines) == 1
    assert lines[0]["reserved_value"] == 3 * 2.0


def test_reserved_value_counts_reservation_once_despite_other_tenant_row(
    client, fake_db
):
    _seed_colliding_items(fake_db)
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert len(lines) == 1
    assert lines[0]["sku"] == "WIDGET"
    assert lines[0]["warehouse_id"] == "w1"
    assert lines[0]["reserved_qty"] == 3
    assert lines[0]["reserved_value"] == 6.0


def test_reserved_value_other_tenant_excludes_foreign_reservation(client, fake_db):
    _seed_colliding_items(fake_db)
    resp = client.get("/reports/reserved-value", headers=TENANT_B)
    assert resp.status_code == 200
    assert resp.json()["lines"] == []


# -- import_snapshot atomicity --


@pytest.fixture
def txn_db(fake_db, monkeypatch):
    """fake_db whose ``transaction()`` has real rollback semantics.

    Mirrors ``app.db.transaction``: on an exception inside the block the
    in-memory items are restored to their state at BEGIN and the error is
    re-raised. ``fail_on`` (1-based) makes the Nth write statement raise.
    """
    state = {"fail_on": None, "writes": 0}
    base = fake_db.transaction

    class _FailingCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=()):
            state["writes"] += 1
            if state["fail_on"] is not None and state["writes"] == state["fail_on"]:
                raise RuntimeError("simulated write failure")
            return self._cur.execute(sql, params)

    class _Conn:
        def __init__(self, conn):
            self._conn = conn

        def cursor(self, cursor_factory=None):
            return _FailingCursor(self._conn.cursor(cursor_factory))

    @contextmanager
    def transaction():
        snapshot = copy.deepcopy(fake_db.items)
        with base() as conn:
            try:
                yield _Conn(conn)
            except Exception:
                fake_db.items[:] = snapshot
                raise

    monkeypatch.setattr(reports_routes, "transaction", transaction)
    fake_db.txn_state = state
    return fake_db


def _seed_three(db):
    for sku in ("A", "B", "C"):
        db.add_item(sku=sku, warehouse_id="w1", quantity=1, tenant_id="tenant-a")


def _quantities(db):
    return {r["sku"]: r["quantity"] for r in db.items}


def test_import_snapshot_malformed_later_entry_writes_nothing(client, txn_db):
    _seed_three(txn_db)
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
    assert _quantities(txn_db) == {"A": 1, "B": 1, "C": 1}
    assert txn_db.txn_state["writes"] == 0


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"sku": "C", "warehouse_id": "w1", "quantity": "lots"},
        {"sku": 5, "warehouse_id": "w1", "quantity": 3},
        {"sku": "C", "warehouse_id": None, "quantity": 3},
        "not-an-object",
    ],
)
def test_import_snapshot_invalid_types_write_nothing(client, txn_db, bad_entry):
    _seed_three(txn_db)
    resp = client.post(
        "/reports/import",
        json={"items": [{"sku": "A", "warehouse_id": "w1", "quantity": 10}, bad_entry]},
        headers=TENANT_A,
    )
    assert resp.status_code == 400
    assert _quantities(txn_db) == {"A": 1, "B": 1, "C": 1}


def test_import_snapshot_write_failure_rolls_back_all(client, txn_db):
    _seed_three(txn_db)
    txn_db.txn_state["fail_on"] = 3
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
    assert _quantities(txn_db) == {"A": 1, "B": 1, "C": 1}


def test_import_snapshot_valid_snapshot_applies_every_entry(client, txn_db):
    _seed_three(txn_db)
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
    assert _quantities(txn_db) == {"A": 10, "B": 20, "C": 30}
