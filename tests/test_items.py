import os

TENANT_A = {"X-Tenant-Id": "tenant-a"}
TENANT_B = {"X-Tenant-Id": "tenant-b"}
ADMIN_HEADERS = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}


def test_get_item_missing_tenant_header_is_rejected(client):
    resp = client.get("/items/WIDGET")
    assert resp.status_code in (400, 422)


def test_get_item_found(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    resp = client.get("/items/WIDGET", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sku"] == "WIDGET"
    assert body["quantity"] == 5


def test_get_item_not_found(client, fake_db):
    resp = client.get("/items/NOPE", headers=TENANT_A)
    assert resp.status_code == 404


def test_search_items_returns_page(client, fake_db):
    for i in range(3):
        fake_db.add_item(
            sku=f"SKU{i}",
            name=f"item {i}",
            warehouse_id="w1",
            quantity=1,
            tenant_id="tenant-a",
        )
    resp = client.get("/items", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None


def test_search_items_paginates(client, fake_db):
    for i in range(5):
        fake_db.add_item(
            sku=f"SKU{i}",
            name=f"item {i}",
            warehouse_id="w1",
            quantity=1,
            tenant_id="tenant-a",
        )
    resp = client.get("/items", params={"limit": 2}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    resp2 = client.get(
        "/items", params={"limit": 2, "cursor": body["next_cursor"]}, headers=TENANT_A
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["items"]) == 2
    # the cursor page should not repeat the first item
    first_page_skus = {i["sku"] for i in body["items"]}
    second_page_skus = {i["sku"] for i in body2["items"]}
    assert first_page_skus.isdisjoint(second_page_skus)


def test_search_items_filters_by_warehouse(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w2", quantity=1, tenant_id="tenant-a"
    )
    resp = client.get("/items", params={"warehouse_id": "w1"}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_search_items_scoped_to_tenant(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w1", quantity=1, tenant_id="tenant-b"
    )
    resp = client.get("/items", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_get_stock_returns_quantity(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=42, tenant_id="tenant-a")
    resp = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sku"] == "WIDGET"
    assert body["quantity"] == 42


def test_get_stock_not_found(client, fake_db):
    resp = client.get(
        "/items/NOPE/stock", params={"warehouse_id": "w1"}, headers=TENANT_A
    )
    assert resp.status_code == 404


def test_reserve_stock_happy_path(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    resp = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 3,
            "order_id": "order-1",
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "reserved"
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["quantity"] == 7
    assert len(fake_db.reservations) == 1


def test_reserve_stock_repeat_order_is_a_noop(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    body = {"sku": "WIDGET", "warehouse_id": "w1", "quantity": 3, "order_id": "order-1"}
    first = client.post("/items/reserve", json=body, headers=TENANT_A)
    second = client.post("/items/reserve", json=body, headers=TENANT_A)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "already_reserved"
    # only reserved once
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["quantity"] == 7


def test_reserve_stock_insufficient_quantity(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.post(
        "/items/reserve",
        json={
            "sku": "WIDGET",
            "warehouse_id": "w1",
            "quantity": 5,
            "order_id": "order-1",
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 409


def test_reserve_stock_missing_item(client, fake_db):
    resp = client.post(
        "/items/reserve",
        json={
            "sku": "NOPE",
            "warehouse_id": "w1",
            "quantity": 1,
            "order_id": "order-1",
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 404


def test_patch_item_updates_only_set_fields(client, fake_db):
    fake_db.add_item(
        sku="WIDGET",
        name="Widget",
        warehouse_id="w1",
        quantity=5,
        price=1.5,
        tenant_id="tenant-a",
    )
    resp = client.patch(
        "/items/WIDGET", json={"price": 2.25}, headers={**TENANT_A, **ADMIN_HEADERS}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["price"] == 2.25
    assert body["name"] == "Widget"
    assert body["warehouse_id"] == "w1"
    assert body["quantity"] == 5
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["price"] == 2.25
    assert item["name"] == "Widget"


def test_patch_item_updates_multiple_fields(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    resp = client.patch(
        "/items/WIDGET",
        json={"name": "Gadget", "warehouse_id": "w2"},
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Gadget"
    assert body["warehouse_id"] == "w2"


def test_patch_item_empty_body_returns_unchanged_row(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    resp = client.patch("/items/WIDGET", json={}, headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Widget"


def test_patch_item_not_found(client, fake_db):
    resp = client.patch("/items/NOPE", json={"name": "x"}, headers=TENANT_A)
    assert resp.status_code == 404


def test_patch_item_scoped_to_tenant(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-b"
    )
    resp = client.patch("/items/WIDGET", json={"name": "Hijacked"}, headers=TENANT_A)
    assert resp.status_code == 404
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["name"] == "Widget"


def test_patch_item_negative_price_rejected(client, fake_db):
    fake_db.add_item(
        sku="WIDGET",
        name="Widget",
        warehouse_id="w1",
        quantity=5,
        price=1.0,
        tenant_id="tenant-a",
    )
    resp = client.patch("/items/WIDGET", json={"price": -1}, headers=TENANT_A)
    assert resp.status_code == 422
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["price"] == 1.0


def test_patch_item_missing_tenant_header_is_rejected(client, fake_db):
    resp = client.patch("/items/WIDGET", json={"name": "x"})
    assert resp.status_code in (400, 422)


def test_patch_item_price_requires_admin_token(client, fake_db):
    fake_db.add_item(
        sku="WIDGET",
        name="Widget",
        warehouse_id="w1",
        quantity=5,
        price=1.5,
        tenant_id="tenant-a",
    )
    resp = client.patch("/items/WIDGET", json={"price": 0.01}, headers=TENANT_A)
    assert resp.status_code == 401
    bad = client.patch(
        "/items/WIDGET",
        json={"price": 0.01, "name": "Cheap"},
        headers={**TENANT_A, "X-Admin-Token": "wrong"},
    )
    assert bad.status_code == 401
    item = next(r for r in fake_db.items if r["sku"] == "WIDGET")
    assert item["price"] == 1.5
    assert item["name"] == "Widget"


def test_patch_item_non_price_fields_do_not_require_admin(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    resp = client.patch(
        "/items/WIDGET", json={"name": "Gadget", "price": None}, headers=TENANT_A
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Gadget"


def test_patch_item_records_one_history_row_per_changed_field(client, fake_db):
    fake_db.add_item(
        sku="WIDGET",
        name="Widget",
        warehouse_id="w1",
        quantity=5,
        price=1.5,
        tenant_id="tenant-a",
    )
    resp = client.patch(
        "/items/WIDGET",
        json={"name": "Gadget", "price": 2.0, "warehouse_id": "w1"},
        headers={**TENANT_A, **ADMIN_HEADERS},
    )
    assert resp.status_code == 200
    recorded = {u["field"]: u for u in fake_db.item_updates}
    # warehouse_id was sent with its current value, so nothing changed there.
    assert set(recorded) == {"name", "price"}
    assert recorded["name"]["old_value"] == "Widget"
    assert recorded["name"]["new_value"] == "Gadget"
    assert recorded["price"]["old_value"] == "1.5"
    assert recorded["price"]["new_value"] == "2.0"
    for u in recorded.values():
        assert u["sku"] == "WIDGET"
        assert u["tenant_id"] == "tenant-a"
        assert u["created_at"] is not None


def test_patch_item_rejected_or_empty_records_no_history(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    client.patch("/items/WIDGET", json={}, headers=TENANT_A)
    client.patch("/items/WIDGET", json={"price": 3.0}, headers=TENANT_A)  # 401
    client.patch("/items/NOPE", json={"name": "x"}, headers=TENANT_A)  # 404
    assert fake_db.item_updates == []


def test_item_history_newest_first(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    client.patch("/items/WIDGET", json={"name": "Gadget"}, headers=TENANT_A)
    client.patch("/items/WIDGET", json={"name": "Gizmo"}, headers=TENANT_A)
    resp = client.get("/items/WIDGET/history", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [(r["old_value"], r["new_value"]) for r in body["items"]] == [
        ("Gadget", "Gizmo"),
        ("Widget", "Gadget"),
    ]
    assert body["next_cursor"] is None


def test_item_history_scoped_to_tenant(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-b"
    )
    client.patch("/items/WIDGET", json={"name": "A-name"}, headers=TENANT_A)
    client.patch("/items/WIDGET", json={"name": "B-name"}, headers=TENANT_B)

    body_a = client.get("/items/WIDGET/history", headers=TENANT_A).json()
    body_b = client.get("/items/WIDGET/history", headers=TENANT_B).json()
    assert [r["new_value"] for r in body_a["items"]] == ["A-name"]
    assert [r["new_value"] for r in body_b["items"]] == ["B-name"]

    fake_db.add_item(
        sku="SECRET", name="s", warehouse_id="w1", quantity=1, tenant_id="tenant-b"
    )
    client.patch("/items/SECRET", json={"name": "s2"}, headers=TENANT_B)
    resp = client.get("/items/SECRET/history", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_item_history_paginates(client, fake_db):
    fake_db.add_item(
        sku="WIDGET", name="n0", warehouse_id="w1", quantity=5, tenant_id="tenant-a"
    )
    for i in range(1, 6):
        client.patch("/items/WIDGET", json={"name": f"n{i}"}, headers=TENANT_A)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/items/WIDGET/history", params=params, headers=TENANT_A)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) <= 2
        seen.extend(r["new_value"] for r in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == ["n5", "n4", "n3", "n2", "n1"]


def test_item_history_invalid_cursor_and_limit(client, fake_db):
    resp = client.get(
        "/items/WIDGET/history", params={"cursor": "abc"}, headers=TENANT_A
    )
    assert resp.status_code == 400
    resp = client.get("/items/WIDGET/history", params={"limit": 0}, headers=TENANT_A)
    assert resp.status_code == 422


def test_item_history_missing_tenant_header_is_rejected(client, fake_db):
    resp = client.get("/items/WIDGET/history")
    assert resp.status_code in (400, 422)
