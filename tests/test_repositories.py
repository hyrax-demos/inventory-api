"""Unit tests for the repository layer, driven by the shared ``fake_db`` fixture."""

from datetime import datetime, timedelta

import pytest

from app.repositories import items as items_repo
from app.repositories import reports as reports_repo
from app.repositories import reservations as reservations_repo

T = "tenant-a"
OTHER = "tenant-b"


# -- items -----------------------------------------------------------------


def test_get_by_sku_found_and_tenant_scoped(fake_db):
    fake_db.add_item(sku="W", warehouse_id="w1", quantity=5, tenant_id=T)
    row = items_repo.get_by_sku("W", T)
    assert row["sku"] == "W"
    assert row["quantity"] == 5
    assert items_repo.get_by_sku("W", OTHER) is None
    assert items_repo.get_by_sku("NOPE", T) is None


def test_search_filters_and_limits(fake_db):
    fake_db.add_item(sku="A", name="apple", warehouse_id="w1", quantity=1, tenant_id=T)
    fake_db.add_item(sku="B", name="banana", warehouse_id="w2", quantity=1, tenant_id=T)
    fake_db.add_item(
        sku="C", name="apricot", warehouse_id="w1", quantity=1, tenant_id=T
    )
    fake_db.add_item(
        sku="D", name="apple", warehouse_id="w1", quantity=1, tenant_id=OTHER
    )

    assert [r["sku"] for r in items_repo.search(T, 10)] == ["A", "B", "C"]
    assert [r["sku"] for r in items_repo.search(T, 2)] == ["A", "B"]
    assert [r["sku"] for r in items_repo.search(T, 10, warehouse_id="w1")] == ["A", "C"]
    assert [r["sku"] for r in items_repo.search(T, 10, q="ap")] == ["A", "C"]


def test_search_cursor_is_inclusive(fake_db):
    rows = [
        fake_db.add_item(sku=f"S{i}", warehouse_id="w1", quantity=1, tenant_id=T)
        for i in range(3)
    ]
    got = items_repo.search(T, 10, cursor=rows[1]["id"])
    assert [r["sku"] for r in got] == ["S1", "S2"]


def test_get_quantity(fake_db):
    fake_db.add_item(sku="W", warehouse_id="w1", quantity=7, tenant_id=T)
    assert items_repo.get_quantity("W", "w1", T) == 7
    assert items_repo.get_quantity("W", "w2", T) is None
    assert items_repo.get_quantity("W", "w1", OTHER) is None


def test_decrement_and_increment_quantity(fake_db):
    fake_db.add_item(sku="W", warehouse_id="w1", quantity=10, tenant_id=T)
    assert items_repo.decrement_quantity("W", "w1", T, 3) == 1
    assert fake_db.items[0]["quantity"] == 7
    assert items_repo.increment_quantity("W", "w1", T, 5) == 1
    assert fake_db.items[0]["quantity"] == 12
    assert items_repo.increment_quantity("W", "w1", OTHER, 5) == 0
    assert fake_db.items[0]["quantity"] == 12


def test_adjust_quantity_all_warehouses(fake_db):
    fake_db.add_item(sku="W", warehouse_id="w1", quantity=1, tenant_id=T)
    fake_db.add_item(sku="W", warehouse_id="w2", quantity=2, tenant_id=T)
    fake_db.add_item(sku="W", warehouse_id="w1", quantity=3, tenant_id=OTHER)
    assert items_repo.adjust_quantity_all_warehouses("W", T, 4) == 2
    assert [r["quantity"] for r in fake_db.items] == [5, 6, 3]


def test_set_quantity(fake_db):
    fake_db.add_item(sku="W", warehouse_id="w1", quantity=1, tenant_id=T)
    assert items_repo.set_quantity("W", "w1", T, 50) == 1
    assert fake_db.items[0]["quantity"] == 50
    assert items_repo.set_quantity("NOPE", "w1", T, 50) == 0


def test_set_price(fake_db):
    fake_db.add_item(sku="W", warehouse_id="w1", quantity=1, price=1.0, tenant_id=T)
    assert items_repo.set_price("W", "w1", T, 9.99) == 1
    assert fake_db.items[0]["price"] == 9.99
    assert items_repo.set_price("W", "w1", OTHER, 1.0) == 0


def test_reset_all_quantities(fake_db):
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=9, tenant_id=T)
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=4, tenant_id=OTHER)
    assert items_repo.reset_all_quantities(T) == 1
    assert [r["quantity"] for r in fake_db.items] == [0, 4]


def test_delete_by_id(fake_db):
    row = fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id=T)
    assert items_repo.delete_by_id(row["id"], OTHER) == 0
    assert items_repo.delete_by_id(row["id"], T) == 1
    assert fake_db.items == []


def test_update_item_fields(fake_db):
    row = fake_db.add_item(
        sku="A", name="old", warehouse_id="w1", quantity=1, tenant_id=T
    )
    assert (
        items_repo.update_item_fields(row["id"], T, {"name": "new", "price": 2.5}) == 1
    )
    assert fake_db.items[0]["name"] == "new"
    assert fake_db.items[0]["price"] == 2.5
    assert items_repo.update_item_fields("missing", T, {"name": "x"}) == 0


def test_update_item_fields_rejects_empty_and_unknown_columns(fake_db):
    row = fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id=T)
    with pytest.raises(ValueError):
        items_repo.update_item_fields(row["id"], T, {})
    with pytest.raises(ValueError):
        items_repo.update_item_fields(row["id"], T, {"quantity": 999})
    assert fake_db.items[0]["quantity"] == 1


# -- reports ---------------------------------------------------------------


def test_low_stock(fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=5, tenant_id=T)
    fake_db.add_item(sku="B", name="b", warehouse_id="w1", quantity=2, tenant_id=T)
    fake_db.add_item(sku="C", name="c", warehouse_id="w1", quantity=50, tenant_id=T)
    fake_db.add_item(sku="D", name="d", warehouse_id="w1", quantity=1, tenant_id=OTHER)
    rows = reports_repo.low_stock(T, 10)
    assert [r["sku"] for r in rows] == ["B", "A"]
    assert set(rows[0]) == {"sku", "name", "warehouse_id", "quantity"}


def test_movements_since(fake_db):
    now = datetime.now()
    fake_db.add_movement(
        sku="OLD",
        warehouse_id="w1",
        delta=1,
        created_at=now - timedelta(days=2),
        tenant_id=T,
    )
    fake_db.add_movement(
        sku="NEW", warehouse_id="w1", delta=-1, created_at=now, tenant_id=T
    )
    fake_db.add_movement(
        sku="X", warehouse_id="w1", delta=-1, created_at=now, tenant_id=OTHER
    )
    rows = reports_repo.movements_since(T, now - timedelta(days=1))
    assert [r["sku"] for r in rows] == ["NEW"]


def test_reserved_value_by_sku(fake_db):
    fake_db.add_item(sku="W", warehouse_id="w1", quantity=100, price=2.0, tenant_id=T)
    fake_db.add_item(sku="V", warehouse_id="w1", quantity=100, price=10.0, tenant_id=T)
    fake_db.add_reservation(
        order_id="o1", tenant_id=T, sku="W", warehouse_id="w1", quantity=3
    )
    fake_db.add_reservation(
        order_id="o2", tenant_id=T, sku="W", warehouse_id="w1", quantity=2
    )
    fake_db.add_reservation(
        order_id="o3", tenant_id=T, sku="V", warehouse_id="w1", quantity=3
    )
    fake_db.add_reservation(
        order_id="o4", tenant_id=OTHER, sku="W", warehouse_id="w1", quantity=9
    )
    lines = reports_repo.reserved_value_by_sku(T)
    # Highest reserved value first: V = 3 * 10.0, W = 5 * 2.0.
    assert [(ln["sku"], ln["reserved_qty"], ln["reserved_value"]) for ln in lines] == [
        ("V", 3, 30.0),
        ("W", 5, 10.0),
    ]


# -- reservations ----------------------------------------------------------


def test_reservation_exists(fake_db):
    fake_db.add_reservation(
        order_id="o1", tenant_id=T, sku="W", warehouse_id="w1", quantity=1
    )
    assert reservations_repo.exists("o1", T) is True
    assert reservations_repo.exists("o1", OTHER) is False
    assert reservations_repo.exists("o2", T) is False


def test_reservation_create_and_get(fake_db):
    assert reservations_repo.get("o1", T) is None
    assert reservations_repo.create("o1", T, "W", "w1", 4) == 1
    row = reservations_repo.get("o1", T)
    assert row["sku"] == "W"
    assert row["warehouse_id"] == "w1"
    assert row["quantity"] == 4
    assert reservations_repo.get("o1", OTHER) is None


def test_reservation_delete(fake_db):
    fake_db.add_reservation(
        order_id="o1", tenant_id=T, sku="W", warehouse_id="w1", quantity=1
    )
    assert reservations_repo.delete("o1", OTHER) == 0
    assert reservations_repo.delete("o1", T) == 1
    assert fake_db.reservations == []
