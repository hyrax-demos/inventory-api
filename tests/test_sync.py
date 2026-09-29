import os

import pytest

TENANT_A = {"X-Tenant-Id": "tenant-a", "X-Admin-Token": os.environ["ADMIN_TOKEN"]}


def test_sync_prices_requires_admin_token(client, fake_db):
    # No network call is reached: the admin dependency runs before the body.
    resp = client.post("/sync/prices")
    assert resp.status_code == 401


def test_sync_single_item_happy_path(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=1, price=1.0, tenant_id="tenant-a"
    )
    resp = client.post(
        "/sync/item/WIDGET",
        params={"warehouse_id": "w1", "price": 9.99},
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert fake_db.items[0]["price"] == 9.99


def test_sync_single_item_not_found(client, fake_db):
    resp = client.post(
        "/sync/item/NOPE",
        params={"warehouse_id": "w1", "price": 1.0},
        headers=TENANT_A,
    )
    assert resp.status_code == 404


def test_release_reservation_happy_path(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert resp.json()["released"] == 3
    assert fake_db.items[0]["quantity"] == 8


def test_release_reservation_not_found(client, fake_db):
    resp = client.post(
        "/reservations/does-not-exist/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404


def _rollback_transaction(fake_db, fail_on):
    """A transaction() stand-in with real rollback over FakeDB's stores.

    Snapshots items and reservations on entry, raises when a statement starting
    with ``fail_on`` runs, and restores the snapshot if the block raises, the
    same way ``app.db.transaction`` rolls back the connection on error.
    """
    import copy
    from contextlib import contextmanager

    class _Cursor:
        def __init__(self):
            self._rows: list = []
            self.rowcount = 0

        def execute(self, sql, params=()):
            if sql.startswith(fail_on):
                raise RuntimeError(f"simulated failure: {fail_on}")
            if "RETURNING" in sql:
                self._rows = fake_db.execute_returning(sql, params)
                self.rowcount = len(self._rows)
            else:
                self.rowcount = fake_db.execute(sql, params)

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class _Conn:
        def cursor(self, cursor_factory=None):
            return _Cursor()

    @contextmanager
    def transaction():
        items = copy.deepcopy(fake_db.items)
        reservations = copy.deepcopy(fake_db.reservations)
        try:
            yield _Conn()
        except Exception:
            fake_db.items = items
            fake_db.reservations = reservations
            raise

    return transaction


def _seed_reservation(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def test_release_reservation_restores_stock_and_deletes_row(client, fake_db):
    _seed_reservation(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def test_release_reservation_keeps_row_when_stock_restore_fails(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import sync as sync_routes

    _seed_reservation(fake_db)
    monkeypatch.setattr(
        sync_routes,
        "transaction",
        _rollback_transaction(fake_db, "UPDATE items SET quantity = quantity + %s"),
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 500
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1
    assert fake_db.reservations[0]["order_id"] == "order-1"


def test_release_reservation_stock_restore_failure_propagates(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import sync as sync_routes

    _seed_reservation(fake_db)
    monkeypatch.setattr(
        sync_routes,
        "transaction",
        _rollback_transaction(fake_db, "UPDATE items SET quantity = quantity + %s"),
    )
    client = TestClient(app)
    with pytest.raises(RuntimeError, match="simulated failure"):
        client.post(
            "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
        )
    assert len(fake_db.reservations) == 1


def test_release_reservation_rolls_back_stock_when_delete_fails(fake_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import sync as sync_routes

    _seed_reservation(fake_db)
    monkeypatch.setattr(
        sync_routes,
        "transaction",
        _rollback_transaction(fake_db, "DELETE FROM reservations"),
    )
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 500
    # The restore ran inside the same transaction and was rolled back with it.
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_reservation_twice_restores_stock_once(client, fake_db):
    _seed_reservation(fake_db)
    headers = {"X-Tenant-Id": "tenant-a"}

    first = client.post("/reservations/order-1/release", headers=headers)
    assert first.status_code == 200
    assert first.json()["released"] == 3
    assert fake_db.items[0]["quantity"] == 8

    second = client.post("/reservations/order-1/release", headers=headers)
    assert second.status_code == 404
    # Stock was restored exactly once.
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def test_release_missing_reservation_does_not_touch_stock(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5


def test_release_other_tenants_reservation_is_404_and_keeps_stock(client, fake_db):
    _seed_reservation(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-b"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_after_failed_restore_can_be_retried_once(fake_db, monkeypatch):
    """A rolled-back release leaves the reservation claimable; the retry
    restores stock once and a further release is a 404."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import sync as sync_routes

    _seed_reservation(fake_db)
    headers = {"X-Tenant-Id": "tenant-a"}
    with monkeypatch.context() as m:
        m.setattr(
            sync_routes,
            "transaction",
            _rollback_transaction(fake_db, "UPDATE items SET quantity = quantity + %s"),
        )
        failing = TestClient(app, raise_server_exceptions=False)
        assert (
            failing.post("/reservations/order-1/release", headers=headers).status_code
            == 500
        )

    client = TestClient(app)
    assert (
        client.post("/reservations/order-1/release", headers=headers).status_code == 200
    )
    assert fake_db.items[0]["quantity"] == 8
    assert (
        client.post("/reservations/order-1/release", headers=headers).status_code == 404
    )
    assert fake_db.items[0]["quantity"] == 8


def _get_stock(client, tenant, warehouse_id, sku="WIDGET"):
    resp = client.get(
        f"/items/{sku}/stock",
        params={"warehouse_id": warehouse_id},
        headers={"X-Tenant-Id": tenant},
    )
    assert resp.status_code == 200
    return resp.json()["quantity"]


def test_release_invalidates_stock_cache_for_get_stock(client, fake_db):
    _seed_reservation(fake_db)
    # Prime the cache that GET /items/{sku}/stock reads.
    assert _get_stock(client, "tenant-a", "w1") == 5

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200

    # The freshly restored quantity, not the cached 5.
    assert _get_stock(client, "tenant-a", "w1") == 8


def test_release_does_not_evict_other_tenant_or_warehouse_stock_cache(client, fake_db):
    from app import cache

    _seed_reservation(fake_db)
    fake_db.add_item(sku="WIDGET", warehouse_id="w2", quantity=11, tenant_id="tenant-a")
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=7, tenant_id="tenant-b")

    # Each tenant/warehouse gets its own cache entry and its own quantity.
    assert _get_stock(client, "tenant-a", "w1") == 5
    assert _get_stock(client, "tenant-a", "w2") == 11
    assert _get_stock(client, "tenant-b", "w1") == 7

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200

    assert cache.get(cache.stock_key("tenant-a", "WIDGET", "w1")) is None
    assert cache.get(cache.stock_key("tenant-a", "WIDGET", "w2")) == 11
    assert cache.get(cache.stock_key("tenant-b", "WIDGET", "w1")) == 7
    assert _get_stock(client, "tenant-a", "w1") == 8
    assert _get_stock(client, "tenant-b", "w1") == 7


def test_release_404_leaves_stock_cache_untouched(client, fake_db):
    from app import cache

    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    assert _get_stock(client, "tenant-a", "w1") == 5
    resp = client.post(
        "/reservations/missing/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert cache.get(cache.stock_key("tenant-a", "WIDGET", "w1")) == 5


def test_stock_key_components_cannot_collide():
    from app import cache

    assert cache.stock_key("a:b", "c", "d") != cache.stock_key("a", "b:c", "d")
    assert cache.stock_key("t", "s", "w") != cache.stock_key("t", "s", "w2")
