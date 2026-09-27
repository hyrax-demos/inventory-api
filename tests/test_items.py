TENANT_A = {"X-Tenant-Id": "tenant-a"}
TENANT_B = {"X-Tenant-Id": "tenant-b"}


def test_get_item_missing_tenant_header_is_rejected(client):
    resp = client.get("/items/WIDGET")
    assert resp.status_code in (400, 422)


def test_get_item_found(client, fake_db):
    fake_db.add_item(sku="WIDGET", name="Widget", warehouse_id="w1", quantity=5, tenant_id="tenant-a")
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
        fake_db.add_item(sku=f"SKU{i}", name=f"item {i}", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.get("/items", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None


def test_search_items_paginates(client, fake_db):
    for i in range(5):
        fake_db.add_item(sku=f"SKU{i}", name=f"item {i}", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    resp = client.get("/items", params={"limit": 2}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    resp2 = client.get("/items", params={"limit": 2, "cursor": body["next_cursor"]}, headers=TENANT_A)
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["items"]) == 2
    # the cursor page should not repeat the first item
    first_page_skus = {i["sku"] for i in body["items"]}
    second_page_skus = {i["sku"] for i in body2["items"]}
    assert first_page_skus.isdisjoint(second_page_skus)


def test_search_items_filters_by_warehouse(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", name="b", warehouse_id="w2", quantity=1, tenant_id="tenant-a")
    resp = client.get("/items", params={"warehouse_id": "w1"}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_search_items_scoped_to_tenant(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", name="b", warehouse_id="w1", quantity=1, tenant_id="tenant-b")
    resp = client.get("/items", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def _walk_pages(client, params, headers):
    """Follow next_cursor from the first page to the last; return (ids, pages)."""
    ids: list[str] = []
    pages = 0
    cursor = None
    while True:
        query = dict(params)
        if cursor is not None:
            query["cursor"] = cursor
        resp = client.get("/items", params=query, headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        pages += 1
        assert len(body["items"]) <= params["limit"]
        ids.extend(i["id"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return ids, pages
        # A non-final page must be full, and the cursor must name its last item.
        assert len(body["items"]) == params["limit"]
        assert cursor == body["items"][-1]["id"]
        assert pages <= 100, "pagination did not terminate"


def _seed_mixed(fake_db):
    # Spread items across tenants, warehouses and names so every filter
    # removes some rows from the middle of the id sequence.
    for i in range(11):
        fake_db.add_item(
            sku=f"SKU{i}",
            name=("red widget " if i % 3 else "blue gadget ") + str(i),
            warehouse_id="w1" if i % 2 else "w2",
            quantity=1,
            tenant_id="tenant-b" if i % 4 == 0 else "tenant-a",
        )


def test_search_items_page_boundary_has_no_duplicate(client, fake_db):
    for i in range(5):
        fake_db.add_item(sku=f"SKU{i}", name=f"item {i}", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    first = client.get("/items", params={"limit": 2}, headers=TENANT_A).json()
    second = client.get(
        "/items", params={"limit": 2, "cursor": first["next_cursor"]}, headers=TENANT_A
    ).json()
    assert [i["sku"] for i in first["items"]] == ["SKU0", "SKU1"]
    assert [i["sku"] for i in second["items"]] == ["SKU2", "SKU3"]


def test_search_items_walk_returns_every_item_exactly_once(client, fake_db):
    _seed_mixed(fake_db)
    for warehouse_id, q in [("", ""), ("w1", ""), ("", "red"), ("w2", "red"), ("w1", "blue")]:
        expected = sorted(
            r["id"]
            for r in fake_db.items
            if r["tenant_id"] == "tenant-a"
            and (not warehouse_id or r["warehouse_id"] == warehouse_id)
            and (not q or q in r["name"])
        )
        assert expected, (warehouse_id, q)
        for limit in range(1, len(fake_db.items) + 2):
            params = {"limit": limit}
            if warehouse_id:
                params["warehouse_id"] = warehouse_id
            if q:
                params["q"] = q
            ids, pages = _walk_pages(client, params, TENANT_A)
            assert ids == expected, (warehouse_id, q, limit)
            assert pages == max(1, -(-len(expected) // limit)), (warehouse_id, q, limit)


def test_search_items_exact_multiple_of_limit_has_no_empty_trailing_page(client, fake_db):
    for i in range(4):
        fake_db.add_item(sku=f"SKU{i}", name=f"item {i}", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    ids, pages = _walk_pages(client, {"limit": 2}, TENANT_A)
    assert ids == ["1", "2", "3", "4"]
    assert pages == 2


def test_search_items_cursor_does_not_leak_other_tenants(client, fake_db):
    _seed_mixed(fake_db)
    ids, _ = _walk_pages(client, {"limit": 1}, TENANT_B)
    assert ids == sorted(r["id"] for r in fake_db.items if r["tenant_id"] == "tenant-b")


def test_search_items_rejects_non_positive_limit(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    for limit in (0, -1):
        resp = client.get("/items", params={"limit": limit}, headers=TENANT_A)
        assert resp.status_code == 422


def test_get_stock_returns_quantity(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=42, tenant_id="tenant-a")
    resp = client.get("/items/WIDGET/stock", params={"warehouse_id": "w1"}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sku"] == "WIDGET"
    assert body["quantity"] == 42


def test_get_stock_not_found(client, fake_db):
    resp = client.get("/items/NOPE/stock", params={"warehouse_id": "w1"}, headers=TENANT_A)
    assert resp.status_code == 404


def test_reserve_stock_happy_path(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=10, tenant_id="tenant-a")
    resp = client.post(
        "/items/reserve",
        json={"sku": "WIDGET", "warehouse_id": "w1", "quantity": 3, "order_id": "order-1"},
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
        json={"sku": "WIDGET", "warehouse_id": "w1", "quantity": 5, "order_id": "order-1"},
        headers=TENANT_A,
    )
    assert resp.status_code == 409


def test_reserve_stock_missing_item(client, fake_db):
    resp = client.post(
        "/items/reserve",
        json={"sku": "NOPE", "warehouse_id": "w1", "quantity": 1, "order_id": "order-1"},
        headers=TENANT_A,
    )
    assert resp.status_code == 404
