"""Atomicity of POST /reservations/{order_id}/release.

The stock restore and the reservation delete must commit together or not at
all. These tests route the handler through the real ``app.db.transaction``
helper, backed by a fake connection that stages writes and applies them to
the in-memory FakeDB only on ``commit()``. That gives true rollback semantics
without needing Postgres.
"""

import pytest
from fastapi.testclient import TestClient

from app import cache
from app import db as db_module
from app.main import app
from app.routes import sync as sync_routes

TENANT_A = {"X-Tenant-Id": "tenant-a"}


class _StagingCursor:
    def __init__(self, conn: "_StagingConnection"):
        self._conn = conn
        self.rowcount = 0
        self._rows: list = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        if self._conn.fail_on and sql.startswith(self._conn.fail_on):
            raise RuntimeError(f"simulated DB failure on: {sql}")
        # A ``... RETURNING`` write reports the rows it would affect straight
        # away (as Postgres does inside the open transaction), while the write
        # itself is still only staged until commit.
        self._rows = (
            self._conn._fake_db.returning_rows(sql, params)
            if "RETURNING" in sql
            else []
        )
        self._conn.pending.append((sql, params))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        pass


class _StagingConnection:
    """Buffers writes; applies them to the FakeDB only on commit."""

    def __init__(self, fake_db, fail_on: str | None = None):
        self._fake_db = fake_db
        self.fail_on = fail_on
        self.pending: list[tuple[str, tuple]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return _StagingCursor(self)

    def commit(self) -> None:
        for sql, params in self.pending:
            self._fake_db.execute(sql, params)
        self.pending.clear()
        self.committed = True

    def rollback(self) -> None:
        self.pending.clear()
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def staging(monkeypatch, fake_db):
    """Use the real transaction() helper over a staging fake connection.

    Returns a factory: call it with ``fail_on=<SQL prefix>`` to make that
    statement raise inside the transaction.
    """
    conns: list[_StagingConnection] = []

    def install(fail_on: str | None = None):
        def get_connection():
            conn = _StagingConnection(fake_db, fail_on=fail_on)
            conns.append(conn)
            return conn

        monkeypatch.setattr(db_module, "get_connection", get_connection)
        monkeypatch.setattr(sync_routes, "transaction", db_module.transaction)
        return conns

    return install


def _seed(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def test_release_restores_stock_and_removes_reservation(client, fake_db, staging):
    conns = staging()
    _seed(fake_db)

    resp = client.post("/reservations/order-1/release", headers=TENANT_A)

    assert resp.status_code == 200
    assert resp.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
    # Both writes went through a single committed transaction.
    assert len(conns) == 1
    assert conns[0].committed and not conns[0].rolled_back and conns[0].closed


def test_release_invalidates_stock_cache_after_commit(client, fake_db, staging):
    staging()
    _seed(fake_db)
    cache.put(cache.stock_key("WIDGET"), {"quantity": 5})

    resp = client.post("/reservations/order-1/release", headers=TENANT_A)

    assert resp.status_code == 200
    assert cache.get(cache.stock_key("WIDGET")) is None


@pytest.mark.parametrize(
    "fail_on",
    ["UPDATE items SET quantity = quantity + %s", "DELETE FROM reservations"],
    ids=["stock-restore-fails", "reservation-delete-fails"],
)
def test_release_failure_rolls_back_everything(fake_db, staging, fail_on):
    conns = staging(fail_on=fail_on)
    _seed(fake_db)
    cache.put(cache.stock_key("WIDGET"), {"quantity": 5})
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/reservations/order-1/release", headers=TENANT_A)

    assert resp.status_code == 500
    # Stock unchanged and the reservation row still present.
    assert fake_db.items[0]["quantity"] == 5
    assert fake_db.reservations == [
        {
            "order_id": "order-1",
            "tenant_id": "tenant-a",
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
        }
    ]
    assert len(conns) == 1
    assert conns[0].rolled_back and not conns[0].committed and conns[0].closed
    # No cache invalidation for a release that did not happen.
    assert cache.get(cache.stock_key("WIDGET")) == {"quantity": 5}


def test_release_failure_propagates_exception(fake_db, staging, client):
    staging(fail_on="UPDATE items SET quantity = quantity + %s")
    _seed(fake_db)

    with pytest.raises(RuntimeError, match="simulated DB failure"):
        client.post("/reservations/order-1/release", headers=TENANT_A)

    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_twice_restores_stock_exactly_once(client, fake_db, staging):
    staging()
    _seed(fake_db)

    first = client.post("/reservations/order-1/release", headers=TENANT_A)
    assert first.status_code == 200
    assert first.json() == {"order_id": "order-1", "released": 3}
    assert fake_db.items[0]["quantity"] == 8
    cache.put(cache.stock_key("WIDGET"), {"quantity": 8})

    second = client.post("/reservations/order-1/release", headers=TENANT_A)
    assert second.status_code == 404
    assert second.json() == {"detail": "no such reservation"}
    # Restored exactly once.
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []
    # Nothing released, so the cache is left alone.
    assert cache.get(cache.stock_key("WIDGET")) == {"quantity": 8}


def test_release_unknown_reservation_is_404_and_leaves_stock(client, fake_db, staging):
    conns = staging()
    _seed(fake_db)
    cache.put(cache.stock_key("WIDGET"), {"quantity": 5})

    resp = client.post("/reservations/no-such-order/release", headers=TENANT_A)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "no such reservation"}
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    # Nothing was committed and the cache was not invalidated.
    assert len(conns) == 1 and not conns[0].committed and conns[0].closed
    assert cache.get(cache.stock_key("WIDGET")) == {"quantity": 5}


def test_release_other_tenants_reservation_is_404(client, fake_db, staging):
    staging()
    _seed(fake_db)
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-b")

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-b"}
    )

    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert fake_db.items[1]["quantity"] == 10
    assert fake_db.reservations == [
        {
            "order_id": "order-1",
            "tenant_id": "tenant-a",
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
        }
    ]


def test_release_twice_via_default_fake_transaction(client, fake_db):
    _seed(fake_db)

    first = client.post("/reservations/order-1/release", headers=TENANT_A)
    second = client.post("/reservations/order-1/release", headers=TENANT_A)
    assert first.status_code == 200
    assert second.status_code == 404
    assert fake_db.items[0]["quantity"] == 8


# -- Stock cache correctness: release vs. GET /items/{sku}/stock --


def _get_stock(client, headers, sku="WIDGET", warehouse_id="w1"):
    resp = client.get(
        f"/items/{sku}/stock", params={"warehouse_id": warehouse_id}, headers=headers
    )
    assert resp.status_code == 200
    return resp.json()["quantity"]


def test_release_refreshes_cached_stock_read(client, fake_db, staging):
    staging()
    _seed(fake_db)

    # Prime the real cache through the reader.
    assert _get_stock(client, TENANT_A) == 5
    assert cache.get(cache.stock_cache_key("tenant-a", "w1", "WIDGET")) == 5

    resp = client.post("/reservations/order-1/release", headers=TENANT_A)
    assert resp.status_code == 200

    # The exact key the reader uses was dropped, so the next read is fresh.
    assert cache.get(cache.stock_cache_key("tenant-a", "w1", "WIDGET")) is None
    assert _get_stock(client, TENANT_A) == 8


def test_release_refreshes_cached_stock_read_default_fake(client, fake_db):
    _seed(fake_db)
    assert _get_stock(client, TENANT_A) == 5

    assert (
        client.post("/reservations/order-1/release", headers=TENANT_A).status_code
        == 200
    )

    assert _get_stock(client, TENANT_A) == 8


def test_release_does_not_disturb_other_tenants_or_warehouses(client, fake_db, staging):
    staging()
    _seed(fake_db)
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=20, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=50, tenant_id="tenant-b")
    tenant_b = {"X-Tenant-Id": "tenant-b"}

    # Each tenant + warehouse sees its own quantity, not a shared cache entry.
    assert _get_stock(client, TENANT_A, warehouse_id="w1") == 5
    assert _get_stock(client, TENANT_A, warehouse_id="w2") == 20
    assert _get_stock(client, tenant_b, warehouse_id="w1") == 50

    resp = client.post("/reservations/order-1/release", headers=TENANT_A)
    assert resp.status_code == 200

    # Unrelated entries stay cached and still hold correct values.
    assert cache.get(cache.stock_cache_key("tenant-a", "w2", "WIDGET")) == 20
    assert cache.get(cache.stock_cache_key("tenant-b", "w1", "WIDGET")) == 50
    assert _get_stock(client, TENANT_A, warehouse_id="w1") == 8
    assert _get_stock(client, TENANT_A, warehouse_id="w2") == 20
    assert _get_stock(client, tenant_b, warehouse_id="w1") == 50


def test_release_invalidates_using_reservation_row_warehouse(client, fake_db, staging):
    """The invalidated key comes from the released row, not from request input."""
    staging()
    fake_db.add_item(sku="GADGET", warehouse_id="w9", quantity=1, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-9",
        tenant_id="tenant-a",
        sku="GADGET",
        warehouse_id="w9",
        quantity=4,
    )
    assert _get_stock(client, TENANT_A, sku="GADGET", warehouse_id="w9") == 1

    resp = client.post(
        "/reservations/order-9/release",
        params={"warehouse_id": "w1", "sku": "WIDGET"},
        headers=TENANT_A,
    )
    assert resp.status_code == 200

    assert _get_stock(client, TENANT_A, sku="GADGET", warehouse_id="w9") == 5


def test_failed_release_keeps_cached_stock(fake_db, staging):
    staging(fail_on="UPDATE items SET quantity = quantity + %s")
    _seed(fake_db)
    client = TestClient(app, raise_server_exceptions=False)
    assert _get_stock(client, TENANT_A) == 5

    resp = client.post("/reservations/order-1/release", headers=TENANT_A)
    assert resp.status_code == 500

    assert cache.get(cache.stock_cache_key("tenant-a", "w1", "WIDGET")) == 5
    assert _get_stock(client, TENANT_A) == 5
