"""Response-shape contract for every endpoint in ``app.routes``.

Each test asserts the exact set of keys a response carries, so any field the
repository layer returns but the response model does not declare (``tenant_id``
in particular) is caught if it leaks.
"""

import io
import os
from datetime import datetime

import pytest
from fastapi.routing import APIRoute

from app.routes import admin as admin_routes
from app.routes import items as items_routes
from app.routes import reports as reports_routes
from app.routes import sync as sync_routes

TENANT = {"X-Tenant-Id": "tenant-a"}
ADMIN = {**TENANT, "X-Admin-Token": os.environ["ADMIN_TOKEN"]}

ITEM_KEYS = {"id", "sku", "name", "quantity", "warehouse_id", "price"}


def _seed_item(fake_db, **overrides):
    row = {
        "sku": "WIDGET",
        "name": "Widget",
        "warehouse_id": "w1",
        "quantity": 10,
        "price": 2.5,
        "tenant_id": "tenant-a",
        # A column the API must never expose even if a SELECT * returns it.
        "internal_cost": 1.23,
    }
    row.update(overrides)
    return fake_db.add_item(**row)


# -- every route declares a response model ----------------------------------


_ROUTERS = {
    "admin": admin_routes.router,
    "items": items_routes.router,
    "reports": reports_routes.router,
    "sync": sync_routes.router,
}
_ROUTES = [
    (name, route)
    for name, router in _ROUTERS.items()
    for route in router.routes
    if isinstance(route, APIRoute)
]


@pytest.mark.parametrize(
    "route",
    [r for _, r in _ROUTES],
    ids=[f"{n}:{','.join(sorted(r.methods))} {r.path}" for n, r in _ROUTES],
)
def test_every_route_declares_a_response_model(route):
    assert route.response_model is not None, f"{route.path} has no response_model"


def test_every_route_module_has_routes():
    assert {name for name, _ in _ROUTES} == set(_ROUTERS)
    assert len(_ROUTES) == 15


# -- items ------------------------------------------------------------------


def test_get_item_shape_omits_tenant_and_internal_columns(client, fake_db):
    row = _seed_item(fake_db)
    resp = client.get("/items/WIDGET", headers=TENANT)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == ITEM_KEYS
    assert body == {
        "id": row["id"],
        "sku": "WIDGET",
        "name": "Widget",
        "quantity": 10,
        "warehouse_id": "w1",
        "price": 2.5,
    }


def test_search_items_shape_omits_tenant_and_internal_columns(client, fake_db):
    for i in range(3):
        _seed_item(fake_db, sku=f"S{i}")
    resp = client.get("/items", params={"limit": 2}, headers=TENANT)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"items", "next_cursor"}
    assert isinstance(body["next_cursor"], str)
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert set(item) == ITEM_KEYS
        assert "tenant_id" not in item


def test_get_stock_shape_uncached_and_cached(client, fake_db):
    _seed_item(fake_db, quantity=42)
    expected = {"sku": "WIDGET", "warehouse_id": "w1", "quantity": 42}
    for _ in range(2):  # first call misses the cache, second hits it
        resp = client.get(
            "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT
        )
        assert resp.status_code == 200
        assert resp.json() == expected


def test_reserve_stock_shape(client, fake_db):
    _seed_item(fake_db)
    body = {"sku": "WIDGET", "warehouse_id": "w1", "quantity": 1, "order_id": "o1"}
    first = client.post("/items/reserve", json=body, headers=TENANT)
    second = client.post("/items/reserve", json=body, headers=TENANT)
    assert first.json() == {"order_id": "o1", "status": "reserved"}
    assert second.json() == {"order_id": "o1", "status": "already_reserved"}


# -- reports ----------------------------------------------------------------


def test_low_stock_shape(client, fake_db):
    _seed_item(fake_db, quantity=2)
    resp = client.get("/reports/low-stock", params={"threshold": 5}, headers=TENANT)
    assert resp.status_code == 200
    assert resp.json() == {
        "threshold": 5,
        "items": [
            {"sku": "WIDGET", "name": "Widget", "warehouse_id": "w1", "quantity": 2}
        ],
    }


def test_todays_movements_shape_omits_tenant(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET",
        warehouse_id="w1",
        delta=-2,
        created_at=datetime.now(),  # noqa: DTZ005 (matches route)
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"date", "movements"}
    assert len(body["movements"]) == 1
    movement = body["movements"][0]
    assert set(movement) == {"sku", "warehouse_id", "delta", "created_at"}
    assert movement["delta"] == -2
    datetime.fromisoformat(movement["created_at"])


def test_reserved_value_shape(client, fake_db):
    _seed_item(fake_db, quantity=100, price=2.0)
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT)
    assert resp.status_code == 200
    assert resp.json() == {
        "lines": [
            {
                "sku": "WIDGET",
                "warehouse_id": "w1",
                "reserved_qty": 3,
                "reserved_value": 6.0,
            }
        ]
    }


def test_import_snapshot_shape(client, fake_db):
    _seed_item(fake_db)
    resp = client.post(
        "/reports/import",
        json={"items": [{"sku": "WIDGET", "warehouse_id": "w1", "quantity": 5}]},
        headers=TENANT,
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": 1, "snapshot": '{"received": 1}'}


# -- sync / reservations ----------------------------------------------------


def test_sync_prices_shape(client, monkeypatch):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        sync_routes.urllib.request,
        "urlopen",
        lambda url, timeout: _Resp(b"feed-body"),
    )
    resp = client.post("/sync/prices", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == {"synced": True, "bytes": len("feed-body")}


def test_sync_single_item_shape(client, fake_db):
    _seed_item(fake_db)
    resp = client.post(
        "/sync/item/WIDGET", params={"warehouse_id": "w1", "price": 9.99}, headers=ADMIN
    )
    assert resp.status_code == 200
    assert resp.json() == {"sku": "WIDGET", "warehouse_id": "w1", "price": 9.99}


def test_release_reservation_shape(client, fake_db):
    _seed_item(fake_db)
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.post("/reservations/o1/release", headers=TENANT)
    assert resp.status_code == 200
    assert resp.json() == {"order_id": "o1", "released": 3}


# -- admin ------------------------------------------------------------------


def test_reset_inventory_shape(client, fake_db):
    _seed_item(fake_db)
    resp = client.post("/admin/items/reset", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == {"reset": True}


def test_delete_item_shape(client, fake_db):
    row = _seed_item(fake_db)
    resp = client.delete(f"/admin/items/{row['id']}", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": row["id"]}


def test_update_item_shape(client, fake_db):
    row = _seed_item(fake_db)
    resp = client.post(
        f"/admin/items/{row['id']}/update",
        json={"name": "new", "price": 3.0},
        headers=ADMIN,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"updated", "fields"}
    assert body["updated"] == row["id"]
    assert sorted(body["fields"]) == ["name", "price"]


def test_bulk_adjust_shape(client, fake_db):
    _seed_item(fake_db)
    resp = client.post(
        "/admin/items/bulk-adjust", json=[{"sku": "WIDGET", "delta": 1}], headers=ADMIN
    )
    assert resp.status_code == 200
    assert resp.json() == {"adjusted": 1}
