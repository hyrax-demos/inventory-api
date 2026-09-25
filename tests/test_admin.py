import os

TENANT_A = {"X-Tenant-Id": "tenant-a", "X-Admin-Token": os.environ["ADMIN_TOKEN"]}


def test_admin_requires_token(client, fake_db):
    resp = client.post("/admin/items/reset", headers={"X-Tenant-Id": "tenant-a"})
    assert resp.status_code == 401


def test_admin_rejects_wrong_token(client, fake_db):
    resp = client.post(
        "/admin/items/reset",
        headers={"X-Tenant-Id": "tenant-a", "X-Admin-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_reset_inventory(client, fake_db):
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=9, tenant_id="tenant-a")
    resp = client.post("/admin/items/reset", headers=TENANT_A)
    assert resp.status_code == 200
    assert fake_db.items[0]["quantity"] == 0


def test_delete_item(client, fake_db):
    row = fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.delete(f"/admin/items/{row['id']}", headers=TENANT_A)
    assert resp.status_code == 200
    assert fake_db.items == []


def test_delete_item_not_found(client, fake_db):
    resp = client.delete("/admin/items/does-not-exist", headers=TENANT_A)
    assert resp.status_code == 404


def test_update_item(client, fake_db):
    row = fake_db.add_item(sku="A", name="old", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.post(
        f"/admin/items/{row['id']}/update",
        json={"name": "new"},
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert fake_db.items[0]["name"] == "new"


def test_update_item_requires_a_patchable_field(client, fake_db):
    row = fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.post(f"/admin/items/{row['id']}/update", json={}, headers=TENANT_A)
    assert resp.status_code == 400


def test_bulk_adjust(client, fake_db):
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.post(
        "/admin/items/bulk-adjust",
        json=[{"sku": "A", "delta": 5}, {"sku": "B", "delta": -1}],
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json()["adjusted"] == 2
    by_sku = {r["sku"]: r["quantity"] for r in fake_db.items}
    assert by_sku == {"A": 6, "B": 0}


def test_bulk_adjust_invalidates_stock_cache_in_every_warehouse(client, fake_db):
    tenant = {"X-Tenant-Id": "tenant-a"}
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="A", warehouse_id="w2", quantity=2, tenant_id="tenant-a")
    for wh in ("w1", "w2"):
        client.get("/items/A/stock", params={"warehouse_id": wh}, headers=tenant)
    resp = client.post("/admin/items/bulk-adjust", json=[{"sku": "A", "delta": 5}], headers=TENANT_A)
    assert resp.status_code == 200
    assert client.get("/items/A/stock", params={"warehouse_id": "w1"}, headers=tenant).json()["quantity"] == 6
    assert client.get("/items/A/stock", params={"warehouse_id": "w2"}, headers=tenant).json()["quantity"] == 7
