import os

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


class _FailingDB:
    """Wraps a FakeDB with a transactional snapshot/rollback and a failure hook."""

    def __init__(self, fake_db, fail_on):
        self.fake_db = fake_db
        self.fail_on = fail_on

    def transaction(self):
        import copy
        from contextlib import contextmanager

        from tests.conftest import _FakeConnection

        fake_db = self.fake_db
        fail_on = self.fail_on

        class _Conn(_FakeConnection):
            def cursor(self, cursor_factory=None):
                inner = super().cursor(cursor_factory)
                orig = inner.execute

                def execute(sql, params=()):
                    if sql.startswith(fail_on):
                        raise RuntimeError("boom")
                    orig(sql, params)

                inner.execute = execute
                return inner

        @contextmanager
        def _tx():
            items = copy.deepcopy(fake_db.items)
            reservations = copy.deepcopy(fake_db.reservations)
            try:
                yield _Conn(fake_db)
            except Exception:
                fake_db.items = items
                fake_db.reservations = reservations
                raise

        return _tx()


def _seed(fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )


def test_release_reservation_removes_row(client, fake_db):
    _seed(fake_db)
    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 200
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def _assert_atomic_failure(monkeypatch, fake_db, fail_on):
    import pytest
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import sync as sync_routes

    _seed(fake_db)
    monkeypatch.setattr(
        sync_routes, "transaction", _FailingDB(fake_db, fail_on).transaction
    )
    with pytest.raises(RuntimeError):
        TestClient(app).post(
            "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
        )
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_reservation_stock_update_failure_keeps_reservation(
    monkeypatch, fake_db
):
    _assert_atomic_failure(monkeypatch, fake_db, "UPDATE items")


def test_release_reservation_delete_failure_rolls_back_stock(monkeypatch, fake_db):
    _assert_atomic_failure(monkeypatch, fake_db, "DELETE FROM reservations")
