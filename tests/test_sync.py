import os

import pytest

TENANT_A = {"X-Tenant-Id": "tenant-a", "X-Admin-Token": os.environ["ADMIN_TOKEN"]}
TENANT_A_HEADER = {"X-Tenant-Id": "tenant-a"}
TENANT_B_HEADER = {"X-Tenant-Id": "tenant-b"}


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


def test_release_reservation_rolls_back_when_stock_restore_fails(
    client, fake_db, monkeypatch
):
    """If the stock-restoring UPDATE fails, the whole release must fail:
    the reservation row must survive and stock must be unchanged, instead of
    the old behaviour where a `finally` block deleted the reservation even
    though its stock was never returned."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )

    real_execute = fake_db.execute

    def _boom(sql, params=()):
        if sql.startswith("UPDATE items SET quantity = quantity + %s"):
            raise RuntimeError("simulated stock-restore failure")
        return real_execute(sql, params)

    monkeypatch.setattr(fake_db, "execute", _boom)

    with pytest.raises(RuntimeError):
        client.post(
            "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
        )

    # Stock was never restored...
    assert fake_db.items[0]["quantity"] == 5
    # ...and the reservation row was not dropped either: no `finally` ran.
    assert fake_db.reservations == [
        {
            "order_id": "order-1",
            "tenant_id": "tenant-a",
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
        }
    ]


def test_release_reservation_twice_restores_stock_only_once(client, fake_db):
    """Releasing the same reservation a second time must be safe: the guard
    lives inside the transaction (a SELECT ... FOR UPDATE on the reservation
    row), so the second call finds no row and must not restore stock again."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )

    first = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert first.status_code == 200
    assert first.json()["released"] == 3
    assert fake_db.items[0]["quantity"] == 8

    second = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert second.status_code == 404

    # Stock reflects exactly one restore, not two.
    assert fake_db.items[0]["quantity"] == 8
    assert fake_db.reservations == []


def test_release_reservation_never_existed_leaves_stock_unchanged(client, fake_db):
    """An id that was never a real reservation gets the same "not found"
    response as an already-released one, and never touches stock."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")

    resp = client.post(
        "/reservations/never-existed/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5


def test_release_reservation_wrong_tenant_is_treated_as_not_found(client, fake_db):
    """A reservation belonging to another tenant must not be released, and
    must not leak its existence via a different status code."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-b")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-b",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )

    resp = client.post(
        "/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert resp.status_code == 404
    assert fake_db.items[0]["quantity"] == 5
    assert len(fake_db.reservations) == 1


def test_release_reservation_called_twice_stock_delta_matches_quantity_once(
    client, fake_db
):
    """Calling the release logic twice in a row must move stock by exactly
    the reservation's quantity, once -- never twice, never zero."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=4,
    )

    before = fake_db.items[0]["quantity"]

    client.post("/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"})
    client.post("/reservations/order-1/release", headers={"X-Tenant-Id": "tenant-a"})

    after = fake_db.items[0]["quantity"]
    assert after - before == 4


def test_release_reservation_invalidates_cached_stock_read(client, fake_db):
    """Populate the real stock cache via GET /items/{sku}/stock, release the
    reservation, and check that a subsequent GET returns the freshly
    restored quantity instead of the value cached before the release."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )

    # Fill the cache with the pre-release quantity.
    before = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A_HEADER
    )
    assert before.status_code == 200
    assert before.json()["quantity"] == 5

    release = client.post("/reservations/order-1/release", headers=TENANT_A_HEADER)
    assert release.status_code == 200
    assert release.json()["released"] == 3

    after = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A_HEADER
    )
    assert after.status_code == 200
    assert after.json()["quantity"] == 8


def test_release_reservation_does_not_disturb_other_skus_cached_stock(client, fake_db):
    """Releasing tenant-a's reservation for WIDGET/w1 must only invalidate
    that SKU's cache entry -- a different SKU's cached stock read (for
    another tenant, another warehouse, or both) must keep returning its own
    correct, unaffected value."""
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
    fake_db.add_item(sku="GADGET", warehouse_id="w2", quantity=20, tenant_id="tenant-a")
    fake_db.add_item(sku="GIZMO", warehouse_id="w1", quantity=50, tenant_id="tenant-b")
    fake_db.add_reservation(
        order_id="order-1",
        tenant_id="tenant-a",
        sku="WIDGET",
        warehouse_id="w1",
        quantity=3,
    )

    # Warm the cache for two unrelated SKUs before releasing tenant-a's
    # WIDGET reservation.
    other_warehouse = client.get(
        "/items/GADGET/stock", params={"warehouse_id": "w2"}, headers=TENANT_A_HEADER
    )
    other_tenant = client.get(
        "/items/GIZMO/stock", params={"warehouse_id": "w1"}, headers=TENANT_B_HEADER
    )
    assert other_warehouse.json()["quantity"] == 20
    assert other_tenant.json()["quantity"] == 50

    release = client.post("/reservations/order-1/release", headers=TENANT_A_HEADER)
    assert release.status_code == 200

    # Both unrelated reads still return their own correct, cached values.
    other_warehouse_after = client.get(
        "/items/GADGET/stock", params={"warehouse_id": "w2"}, headers=TENANT_A_HEADER
    )
    other_tenant_after = client.get(
        "/items/GIZMO/stock", params={"warehouse_id": "w1"}, headers=TENANT_B_HEADER
    )
    assert other_warehouse_after.json()["quantity"] == 20
    assert other_tenant_after.json()["quantity"] == 50
