"""HTTP-level tests for admin soft-delete / restore of items by sku."""

import os

import pytest

ADMIN_A = {"X-Tenant-Id": "tenant-a", "X-Admin-Token": os.environ["ADMIN_TOKEN"]}
ADMIN_B = {"X-Tenant-Id": "tenant-b", "X-Admin-Token": os.environ["ADMIN_TOKEN"]}
READER_A = {"X-Tenant-Id": "tenant-a"}


def _seed(fake_db, tenant_id="tenant-a", sku="WIDGET"):
    return fake_db.add_item(
        sku=sku, tenant_id=tenant_id, name="Widget", warehouse_id="w1", quantity=3
    )


def test_admin_delete_then_second_delete_is_404(client, fake_db):
    row = _seed(fake_db)

    resp = client.delete("/admin/items/WIDGET", headers=ADMIN_A)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "WIDGET"}
    assert row["deleted_at"] is not None
    assert fake_db.items == [row]
    assert client.get("/items/WIDGET", headers=READER_A).status_code == 404

    again = client.delete("/admin/items/WIDGET", headers=ADMIN_A)
    assert again.status_code == 404


@pytest.mark.parametrize(
    "headers",
    [READER_A, {**READER_A, "X-Admin-Token": "wrong"}],
    ids=["no-token", "wrong-token"],
)
def test_non_admin_is_rejected(client, fake_db, headers):
    row = _seed(fake_db)

    assert client.delete("/admin/items/WIDGET", headers=headers).status_code == 401
    assert row["deleted_at"] is None

    row["deleted_at"] = "2024-01-01T00:00:00Z"
    resp = client.post("/admin/items/WIDGET/restore", headers=headers)
    assert resp.status_code == 401
    assert row["deleted_at"] is not None


def test_restore_after_delete_then_restore_live_is_404(client, fake_db):
    row = _seed(fake_db)
    assert client.delete("/admin/items/WIDGET", headers=ADMIN_A).status_code == 200

    resp = client.post("/admin/items/WIDGET/restore", headers=ADMIN_A)
    assert resp.status_code == 200
    assert resp.json() == {"restored": "WIDGET"}
    assert row["deleted_at"] is None
    assert client.get("/items/WIDGET", headers=READER_A).status_code == 200

    live = client.post("/admin/items/WIDGET/restore", headers=ADMIN_A)
    assert live.status_code == 404


def test_unknown_sku_is_404(client, fake_db):
    _seed(fake_db)
    assert client.delete("/admin/items/NOPE", headers=ADMIN_A).status_code == 404
    assert client.post("/admin/items/NOPE/restore", headers=ADMIN_A).status_code == 404


def test_delete_and_restore_are_tenant_scoped(client, fake_db):
    row_a = _seed(fake_db, tenant_id="tenant-a")
    row_b = _seed(fake_db, tenant_id="tenant-b")

    assert client.delete("/admin/items/WIDGET", headers=ADMIN_B).status_code == 200
    assert row_b["deleted_at"] is not None
    assert row_a["deleted_at"] is None

    # Tenant A cannot restore tenant B's deleted item.
    assert (
        client.post("/admin/items/WIDGET/restore", headers=ADMIN_A).status_code == 404
    )
    assert row_b["deleted_at"] is not None


def test_empty_tenant_is_rejected(client, fake_db):
    row = _seed(fake_db)
    headers = {**ADMIN_A, "X-Tenant-Id": ""}
    assert client.delete("/admin/items/WIDGET", headers=headers).status_code == 400
    assert (
        client.post("/admin/items/WIDGET/restore", headers=headers).status_code == 400
    )
    assert row["deleted_at"] is None
