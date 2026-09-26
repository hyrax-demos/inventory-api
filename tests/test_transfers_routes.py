"""Smoke tests proving POST/GET /transfers are wired through to the service.

The DB is replaced by the in-memory transaction store used by the service
tests, so the real service and repo code run underneath the HTTP layer.
"""

import pytest

from app import db
from tests.test_transfers_service import Store

TENANT_A = {"X-Tenant-Id": "tenant-a"}


@pytest.fixture
def store(monkeypatch):
    s = Store()
    monkeypatch.setattr(db, "transaction", s.transaction)
    s.add_item("tenant-a", "w1", "WIDGET", 10)
    s.add_item("tenant-a", "w2", "GADGET", 1)
    return s


def test_post_transfer_returns_201(client, store):
    body = {
        "sku": "WIDGET",
        "source_warehouse_id": "w1",
        "destination_warehouse_id": "w2",
        "quantity": 4,
    }
    resp = client.post("/transfers", json=body, headers=TENANT_A)

    assert resp.status_code == 201
    data = resp.json()
    assert set(data) == {
        "id",
        "sku",
        "source_warehouse_id",
        "destination_warehouse_id",
        "quantity",
        "created_at",
    }
    assert data["sku"] == "WIDGET" and data["quantity"] == 4
    assert store.qty("tenant-a", "w1", "WIDGET") == 6
    assert store.qty("tenant-a", "w2", "WIDGET") == 4


def test_get_transfers_returns_the_item(client, store):
    created = client.post(
        "/transfers",
        json={
            "sku": "WIDGET",
            "source_warehouse_id": "w1",
            "destination_warehouse_id": "w2",
            "quantity": 2,
        },
        headers=TENANT_A,
    ).json()

    resp = client.get("/transfers", headers=TENANT_A)

    assert resp.status_code == 200
    assert resp.json() == {"items": [created], "next_cursor": None}
