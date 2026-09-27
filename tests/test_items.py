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


def _walk_pages(client, headers, **params):
    """Follow next_cursor until exhausted; return (all ids, page count)."""
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
        ids.extend(i["id"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return ids, pages
        assert pages < 100, "pagination did not terminate"


def test_search_items_pages_have_no_duplicates_or_gaps(client, fake_db):
    for n_items in range(13):
        for limit in range(1, 6):
            fake_db.items.clear()
            for i in range(n_items):
                fake_db.add_item(
                    sku=f"SKU{i}", name=f"item {i}", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
                )
            expected = sorted((r["id"] for r in fake_db.items), key=int)
            ids, pages = _walk_pages(client, TENANT_A, limit=limit)
            assert ids == expected, (n_items, limit)
            assert pages == max(1, -(-n_items // limit)), (n_items, limit)


def test_search_items_next_cursor_absent_on_exact_final_page(client, fake_db):
    for i in range(4):
        fake_db.add_item(sku=f"SKU{i}", name=f"item {i}", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    first = client.get("/items", params={"limit": 2}, headers=TENANT_A).json()
    assert first["next_cursor"] == first["items"][-1]["id"]
    second = client.get("/items", params={"limit": 2, "cursor": first["next_cursor"]}, headers=TENANT_A).json()
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None


def test_search_items_pagination_respects_filters_and_tenant(client, fake_db):
    expected = []
    for i in range(15):
        # interleave rows that each filter must exclude
        wh = "w1" if i % 3 else "w2"
        name = f"widget {i}" if i % 2 else f"gadget {i}"
        row = fake_db.add_item(sku=f"A{i}", name=name, warehouse_id=wh, quantity=1, tenant_id="tenant-a")
        fake_db.add_item(sku=f"B{i}", name=name, warehouse_id=wh, quantity=1, tenant_id="tenant-b")
        if wh == "w1" and name.startswith("widget"):
            expected.append(row["id"])
    assert len(expected) > 3
    for limit in (1, 2, 3, 50):
        ids, _ = _walk_pages(client, TENANT_A, limit=limit, warehouse_id="w1", q="widget")
        assert ids == expected, limit


def test_search_items_rejects_non_positive_limit(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    for bad in (0, -1):
        resp = client.get("/items", params={"limit": bad}, headers=TENANT_A)
        assert resp.status_code == 422
