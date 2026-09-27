from datetime import datetime

import conftest
import pytest

TENANT_A = {"X-Tenant-Id": "tenant-a"}
TENANT_B = {"X-Tenant-Id": "tenant-b"}


def test_low_stock_report_happy_path(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=2, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w1", quantity=99, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", params={"threshold": 10}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_low_stock_report_empty_tenant_header_is_rejected(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=2, tenant_id="tenant-a"
    )
    resp = client.get(
        "/reports/low-stock",
        params={"threshold": 10},
        headers={"X-Tenant-Id": ""},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing tenant"


def test_low_stock_report_scoped_to_tenant(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w1", quantity=1, tenant_id="tenant-b"
    )
    resp = client.get("/reports/low-stock", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_todays_movements_returns_recent_entries(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET",
        warehouse_id="w1",
        delta=-2,
        created_at=datetime.now(),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert any(m["sku"] == "WIDGET" for m in body["movements"])


def test_todays_movements_scoped_to_tenant(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET",
        warehouse_id="w1",
        delta=-2,
        created_at=datetime.now(),
        tenant_id="tenant-b",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["movements"] == []


def test_reserved_value_happy_path(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert line["sku"] == "WIDGET"
    assert line["reserved_qty"] == 3


def test_reserved_value_does_not_price_using_other_tenants_item(client, fake_db):
    """A reservation must be priced only using the item row of the SAME tenant,
    even when another tenant has an item at the identical (sku, warehouse_id).
    """
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    # Tenant B has an item at the same (sku, warehouse_id) but a different price,
    # and its own reservation for the same line.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=50, price=9.0, tenant_id="tenant-b"
    )
    fake_db.add_reservation(
        order_id="o2", tenant_id="tenant-b", sku="WIDGET", warehouse_id="w1", quantity=5
    )

    resp_a = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp_a.status_code == 200
    lines_a = resp_a.json()["lines"]
    assert len(lines_a) == 1
    line_a = lines_a[0]
    assert line_a["sku"] == "WIDGET"
    assert line_a["reserved_qty"] == 3
    # 3 * 2.0 (tenant A's price) -- never tenant B's price, and never both summed.
    assert line_a["reserved_value"] == 6.0

    resp_b = client.get("/reports/reserved-value", headers=TENANT_B)
    assert resp_b.status_code == 200
    lines_b = resp_b.json()["lines"]
    assert len(lines_b) == 1
    line_b = lines_b[0]
    assert line_b["sku"] == "WIDGET"
    assert line_b["reserved_qty"] == 5
    # 5 * 9.0 (tenant B's own price) -- unaffected by tenant A's rows.
    assert line_b["reserved_value"] == 45.0


def test_reserved_value_no_item_in_own_tenant_is_unpriced(client, fake_db):
    """If a tenant's reservation has no matching item row in that same tenant,
    it must not be priced using another tenant's item at the same sku/warehouse.
    """
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    # Only tenant B has an item at this (sku, warehouse_id).
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=50, price=9.0, tenant_id="tenant-b"
    )

    resp_a = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp_a.status_code == 200
    assert resp_a.json()["lines"] == []


def test_import_snapshot_happy_path(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.post(
        "/reports/import",
        json={"items": [{"sku": "WIDGET", "warehouse_id": "w1", "quantity": 50}]},
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == 1
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["quantity"] == 50


def test_import_snapshot_rejects_non_list_body(client, fake_db):
    resp = client.post("/reports/import", json={"items": "nope"}, headers=TENANT_A)
    assert resp.status_code == 400


def test_import_snapshot_rejects_malformed_entry(client, fake_db):
    resp = client.post(
        "/reports/import",
        json={"items": [{"sku": "WIDGET", "warehouse_id": "w1"}]},  # missing quantity
        headers=TENANT_A,
    )
    assert resp.status_code == 400


def test_import_snapshot_all_or_nothing_on_trailing_malformed_entry(client, fake_db):
    """A malformed entry at the end of the list must not leave the earlier,
    valid entries' updates applied -- the whole import is rejected and
    nothing is written.
    """
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 100},
                {"sku": "B", "warehouse_id": "w1", "quantity": 200},
                {"sku": "C", "warehouse_id": "w1"},  # missing quantity: malformed
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 400
    item_a = next(r for r in fake_db.items if r["sku"] == "A")
    item_b = next(r for r in fake_db.items if r["sku"] == "B")
    assert item_a["quantity"] == 1
    assert item_b["quantity"] == 2


def test_import_snapshot_all_or_nothing_on_write_failure(client, fake_db, monkeypatch):
    """If a write fails partway through the transaction, no entry's update
    may persist -- not even the ones issued before the failing one.
    """
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
    fake_db.add_item(sku="C", warehouse_id="w1", quantity=3, tenant_id="tenant-a")

    real_cursor_execute = conftest._FakeCursor.execute
    calls = {"n": 0}

    def flaky_cursor_execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("UPDATE"):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated write failure")
        return real_cursor_execute(self, sql, params)

    monkeypatch.setattr(conftest._FakeCursor, "execute", flaky_cursor_execute)

    with pytest.raises(RuntimeError):
        client.post(
            "/reports/import",
            json={
                "items": [
                    {"sku": "A", "warehouse_id": "w1", "quantity": 100},
                    {"sku": "B", "warehouse_id": "w1", "quantity": 200},
                    {"sku": "C", "warehouse_id": "w1", "quantity": 300},
                ]
            },
            headers=TENANT_A,
        )
    item_a = next(r for r in fake_db.items if r["sku"] == "A")
    item_b = next(r for r in fake_db.items if r["sku"] == "B")
    item_c = next(r for r in fake_db.items if r["sku"] == "C")
    assert item_a["quantity"] == 1
    assert item_b["quantity"] == 2
    assert item_c["quantity"] == 3


def test_import_snapshot_applies_every_entry_when_fully_valid(client, fake_db):
    """A fully valid multi-entry snapshot applies every entry's update."""
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
    fake_db.add_item(sku="C", warehouse_id="w1", quantity=3, tenant_id="tenant-a")

    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 100},
                {"sku": "B", "warehouse_id": "w1", "quantity": 200},
                {"sku": "C", "warehouse_id": "w1", "quantity": 300},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == 3
    item_a = next(r for r in fake_db.items if r["sku"] == "A")
    item_b = next(r for r in fake_db.items if r["sku"] == "B")
    item_c = next(r for r in fake_db.items if r["sku"] == "C")
    assert item_a["quantity"] == 100
    assert item_b["quantity"] == 200
    assert item_c["quantity"] == 300
