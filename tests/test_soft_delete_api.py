"""End-to-end soft-delete behavior through the public HTTP endpoints.

Every state change goes through DELETE /admin/items/{sku} and
POST /admin/items/{sku}/restore; every check goes through a read endpoint
(get, search, stock, low-stock report, reserved-value report).
"""

import os

import pytest

TOKEN = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}
READER_A = {"X-Tenant-Id": "tenant-a"}
READER_B = {"X-Tenant-Id": "tenant-b"}
ADMIN_A = {**READER_A, **TOKEN}
ADMIN_B = {**READER_B, **TOKEN}

SKU = "WIDGET"


@pytest.fixture
def seeded(fake_db):
    """Same sku in two tenants, plus a second live sku per tenant so reports
    and search have totals that should shrink by exactly the deleted item."""
    rows = {}
    for tenant, price in (("tenant-a", 2.0), ("tenant-b", 5.0)):
        rows[tenant] = fake_db.add_item(
            sku=SKU,
            tenant_id=tenant,
            name="Widget",
            warehouse_id="w1",
            quantity=3,
            price=price,
        )
        fake_db.add_item(
            sku="GADGET",
            tenant_id=tenant,
            name="Gadget",
            warehouse_id="w1",
            quantity=4,
            price=1.0,
        )
        for sku, qty in ((SKU, 2), ("GADGET", 1)):
            fake_db.add_reservation(
                order_id=f"{tenant}-{sku}",
                tenant_id=tenant,
                sku=sku,
                warehouse_id="w1",
                quantity=qty,
            )
    return rows


# -- read-path helpers --


def _get(client, headers, sku=SKU):
    return client.get(f"/items/{sku}", headers=headers)


def _search_skus(client, headers, **params):
    resp = client.get("/items", params=params, headers=headers)
    assert resp.status_code == 200
    return [i["sku"] for i in resp.json()["items"]]


def _stock(client, headers, sku=SKU):
    return client.get(
        f"/items/{sku}/stock", params={"warehouse_id": "w1"}, headers=headers
    )


def _low_stock(client, headers):
    resp = client.get("/reports/low-stock", params={"threshold": 10}, headers=headers)
    assert resp.status_code == 200
    return resp.json()["items"]


def _reserved_value(client, headers):
    resp = client.get("/reports/reserved-value", headers=headers)
    assert resp.status_code == 200
    return resp.json()["lines"]


def _visible_everywhere(client, headers, sku=SKU) -> dict:
    return {
        "get": _get(client, headers, sku).status_code == 200,
        "search": sku in _search_skus(client, headers),
        "stock": _stock(client, headers, sku).status_code == 200,
        "low_stock": any(i["sku"] == sku for i in _low_stock(client, headers)),
        "reserved_value": any(
            line["sku"] == sku for line in _reserved_value(client, headers)
        ),
    }


ALL_VISIBLE = dict.fromkeys(
    ("get", "search", "stock", "low_stock", "reserved_value"), True
)
NONE_VISIBLE = dict.fromkeys(ALL_VISIBLE, False)


def _delete(client, headers, sku=SKU):
    return client.delete(f"/admin/items/{sku}", headers=headers)


def _restore(client, headers, sku=SKU):
    return client.post(f"/admin/items/{sku}/restore", headers=headers)


# -- exclusion from each read path after delete --


def test_get_item_is_404_after_delete(client, seeded):
    assert _get(client, READER_A).status_code == 200
    assert _delete(client, ADMIN_A).status_code == 200
    assert _get(client, READER_A).status_code == 404


def test_search_omits_deleted_item(client, seeded):
    assert _search_skus(client, READER_A) == [SKU, "GADGET"]
    assert _delete(client, ADMIN_A).status_code == 200
    assert _search_skus(client, READER_A) == ["GADGET"]
    assert _search_skus(client, READER_A, q="widget") == []
    assert _search_skus(client, READER_A, warehouse_id="w1") == ["GADGET"]


def test_search_pagination_skips_deleted_item(client, seeded):
    assert _delete(client, ADMIN_A).status_code == 200
    resp = client.get("/items", params={"limit": 1}, headers=READER_A)
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["GADGET"]
    assert body["next_cursor"] is None


def test_stock_is_404_after_delete(client, seeded):
    assert _delete(client, ADMIN_A).status_code == 200
    assert _stock(client, READER_A).status_code == 404


def test_stock_is_404_after_delete_even_if_previously_cached(client, seeded):
    assert _stock(client, READER_A).json()["quantity"] == 3  # warms the cache
    assert _delete(client, ADMIN_A).status_code == 200
    assert _stock(client, READER_A).status_code == 404


def test_low_stock_report_omits_deleted_item(client, seeded):
    assert {i["sku"] for i in _low_stock(client, READER_A)} == {SKU, "GADGET"}
    assert _delete(client, ADMIN_A).status_code == 200
    items = _low_stock(client, READER_A)
    assert [i["sku"] for i in items] == ["GADGET"]
    assert len(items) == 1


def test_reserved_value_report_omits_deleted_item_and_totals_drop(client, seeded):
    before = _reserved_value(client, READER_A)
    assert {line["sku"] for line in before} == {SKU, "GADGET"}
    # tenant-a prices: WIDGET 2 reserved * 2.0, GADGET 1 reserved * 1.0
    assert sum(line["reserved_value"] for line in before) == pytest.approx(5.0)
    assert sum(line["reserved_qty"] for line in before) == 3

    assert _delete(client, ADMIN_A).status_code == 200

    after = _reserved_value(client, READER_A)
    assert [line["sku"] for line in after] == ["GADGET"]
    assert sum(line["reserved_value"] for line in after) == pytest.approx(1.0)
    assert sum(line["reserved_qty"] for line in after) == 1


# -- restore --


def test_restore_brings_item_back_on_every_read_path(client, seeded):
    assert _delete(client, ADMIN_A).status_code == 200
    assert _visible_everywhere(client, READER_A) == NONE_VISIBLE

    resp = _restore(client, ADMIN_A)
    assert resp.status_code == 200

    assert _visible_everywhere(client, READER_A) == ALL_VISIBLE
    assert _stock(client, READER_A).json()["quantity"] == 3
    assert sum(
        line["reserved_value"] for line in _reserved_value(client, READER_A)
    ) == pytest.approx(5.0)


# -- tenant isolation --


def test_delete_in_tenant_a_leaves_tenant_b_visible_everywhere(client, seeded):
    assert _delete(client, ADMIN_A).status_code == 200

    assert _visible_everywhere(client, READER_B) == ALL_VISIBLE
    assert _visible_everywhere(client, READER_A) == NONE_VISIBLE
    assert seeded["tenant-b"]["deleted_at"] is None


def test_reserved_value_prices_only_callers_own_live_rows(client, seeded):
    assert _delete(client, ADMIN_A).status_code == 200
    # Tenant B's same-sku row must not stand in for tenant A's deleted one.
    assert SKU not in {line["sku"] for line in _reserved_value(client, READER_A)}
    b_lines = {line["sku"]: line for line in _reserved_value(client, READER_B)}
    assert b_lines[SKU]["reserved_value"] == pytest.approx(2 * 5.0)


def test_other_tenants_cached_stock_does_not_leak_deleted_item(client, seeded):
    assert _delete(client, ADMIN_A).status_code == 200
    assert _stock(client, READER_B).status_code == 200  # tenant B warms cache
    assert _stock(client, READER_A).status_code == 404


def test_tenant_b_cannot_restore_or_delete_tenant_a_item(client, fake_db):
    row_a = fake_db.add_item(
        sku=SKU, tenant_id="tenant-a", name="Widget", warehouse_id="w1", quantity=3
    )
    assert _delete(client, ADMIN_A).status_code == 200
    deleted_at = row_a["deleted_at"]
    assert deleted_at is not None

    # Tenant B has no such sku: restore and delete both miss.
    assert _restore(client, ADMIN_B).status_code == 404
    assert _delete(client, ADMIN_B).status_code == 404

    # Tenant A's item stays deleted, with its original timestamp.
    assert row_a["deleted_at"] == deleted_at
    assert _visible_everywhere(client, READER_A) == NONE_VISIBLE
    # And is still restorable by its own tenant.
    assert _restore(client, ADMIN_A).status_code == 200
    assert _get(client, READER_A).status_code == 200


def test_tenant_b_restore_only_touches_its_own_deleted_item(client, seeded):
    assert _delete(client, ADMIN_A).status_code == 200
    # Tenant B's own sku is live, so its restore is a 404 and A stays deleted.
    assert _restore(client, ADMIN_B).status_code == 404
    assert seeded["tenant-a"]["deleted_at"] is not None
    assert _get(client, READER_A).status_code == 404
    assert _visible_everywhere(client, READER_B) == ALL_VISIBLE
