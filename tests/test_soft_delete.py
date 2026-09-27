"""Soft-delete data layer: app.queries.soft_delete_item / restore_item and the
live-items filter applied on every read path (get, search, stock, reports)."""

from app import cache, queries

TENANT_A = {"X-Tenant-Id": "tenant-a"}
TENANT_B = {"X-Tenant-Id": "tenant-b"}


def _seed(fake_db, tenant_id="tenant-a", sku="WIDGET", **kw):
    fields = {"name": "Widget", "warehouse_id": "w1", "quantity": 3, "price": 2.0, **kw}
    return fake_db.add_item(sku=sku, tenant_id=tenant_id, **fields)


def _visible_everywhere(client, sku="WIDGET", headers=TENANT_A) -> dict:
    """Which read paths currently return the item."""
    stock = client.get(
        f"/items/{sku}/stock", params={"warehouse_id": "w1"}, headers=headers
    )
    low = client.get(
        "/reports/low-stock", params={"threshold": 10}, headers=headers
    ).json()
    value = client.get("/reports/reserved-value", headers=headers).json()
    search = client.get("/items", headers=headers).json()
    return {
        "get": client.get(f"/items/{sku}", headers=headers).status_code == 200,
        "search": any(i["sku"] == sku for i in search["items"]),
        "stock": stock.status_code == 200,
        "low_stock": any(i["sku"] == sku for i in low["items"]),
        "reserved_value": any(line["sku"] == sku for line in value["lines"]),
    }


def test_live_items_condition_is_centralized():
    assert queries.live_items() == "deleted_at IS NULL"
    assert queries.live_items("i") == "i.deleted_at IS NULL"


def test_new_items_are_live_by_default(client, fake_db):
    row = _seed(fake_db)
    assert row["deleted_at"] is None
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=2
    )
    assert all(_visible_everywhere(client).values())


def test_soft_delete_hides_item_from_every_read_path(client, fake_db):
    row = _seed(fake_db)
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=2
    )

    assert queries.soft_delete_item("tenant-a", "WIDGET") is True

    assert row["deleted_at"] is not None
    assert fake_db.items == [row]  # never physically removed
    assert _visible_everywhere(client) == {
        "get": False,
        "search": False,
        "stock": False,
        "low_stock": False,
        "reserved_value": False,
    }


def test_soft_delete_invalidates_cached_stock(client, fake_db):
    _seed(fake_db)
    assert (
        client.get(
            "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
        ).status_code
        == 200
    )
    assert cache.get(cache.stock_key("WIDGET")) is not None

    queries.soft_delete_item("tenant-a", "WIDGET")

    resp = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    assert resp.status_code == 404


def test_soft_delete_hides_item_from_reservation_lookup(client, fake_db):
    _seed(fake_db, quantity=10)
    queries.soft_delete_item("tenant-a", "WIDGET")
    resp = client.post(
        "/items/reserve",
        json={"sku": "WIDGET", "warehouse_id": "w1", "quantity": 1, "order_id": "o1"},
        headers=TENANT_A,
    )
    assert resp.status_code == 404


def test_soft_delete_is_not_repeatable(fake_db):
    _seed(fake_db)
    assert queries.soft_delete_item("tenant-a", "WIDGET") is True
    assert queries.soft_delete_item("tenant-a", "WIDGET") is False


def test_soft_delete_missing_sku_affects_nothing(fake_db):
    _seed(fake_db)
    assert queries.soft_delete_item("tenant-a", "NOPE") is False
    assert fake_db.items[0]["deleted_at"] is None


def test_restore_brings_item_back_on_every_read_path(client, fake_db):
    row = _seed(fake_db)
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=2
    )
    queries.soft_delete_item("tenant-a", "WIDGET")

    assert queries.restore_item("tenant-a", "WIDGET") is True

    assert row["deleted_at"] is None
    assert all(_visible_everywhere(client).values())


def test_restore_of_live_or_missing_item_affects_nothing(fake_db):
    _seed(fake_db)
    assert queries.restore_item("tenant-a", "WIDGET") is False
    assert queries.restore_item("tenant-a", "NOPE") is False


def test_soft_delete_is_tenant_isolated(client, fake_db):
    row_a = _seed(fake_db, tenant_id="tenant-a")
    row_b = _seed(fake_db, tenant_id="tenant-b")
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-b", sku="WIDGET", warehouse_id="w1", quantity=2
    )

    assert queries.soft_delete_item("tenant-a", "WIDGET") is True

    assert row_a["deleted_at"] is not None
    assert row_b["deleted_at"] is None
    assert all(_visible_everywhere(client, headers=TENANT_B).values())
    assert not _visible_everywhere(client, headers=TENANT_A)["get"]


def test_restore_is_tenant_isolated(fake_db):
    row_a = _seed(fake_db, tenant_id="tenant-a")
    row_b = _seed(fake_db, tenant_id="tenant-b")
    queries.soft_delete_item("tenant-a", "WIDGET")
    queries.soft_delete_item("tenant-b", "WIDGET")

    assert queries.restore_item("tenant-b", "WIDGET") is True

    assert row_a["deleted_at"] is not None
    assert row_b["deleted_at"] is None


def test_soft_delete_by_other_tenant_cannot_reach_item(fake_db):
    row_a = _seed(fake_db, tenant_id="tenant-a")
    assert queries.soft_delete_item("tenant-b", "WIDGET") is False
    assert row_a["deleted_at"] is None
