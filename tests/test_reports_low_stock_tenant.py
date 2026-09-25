"""low_stock_report rejects an empty X-Tenant-Id header like the items routes."""


def test_low_stock_report_rejects_empty_tenant_header(client, fake_db):
    # A row with an empty tenant_id would otherwise match an empty header.
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400
    assert resp.json() == {"detail": "missing tenant"}


def test_low_stock_report_empty_tenant_matches_items_convention(client, fake_db):
    items_resp = client.get("/items/WIDGET", headers={"X-Tenant-Id": ""})
    reports_resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert reports_resp.status_code == items_resp.status_code == 400
    assert reports_resp.json() == items_resp.json()


def test_low_stock_report_valid_tenant_still_ok(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=3, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w1", quantity=50, tenant_id="tenant-a"
    )
    resp = client.get(
        "/reports/low-stock",
        params={"threshold": 10},
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["threshold"] == 10
    assert [i["sku"] for i in body["items"]] == ["A"]
