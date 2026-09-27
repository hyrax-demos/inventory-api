TENANT_A = {"X-Tenant-Id": "tenant-a"}
TENANT_B = {"X-Tenant-Id": "tenant-b"}


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


def _walk_pages(client, headers, **params):
    """Follow next_cursor until exhausted; return ids in the order returned."""
    ids: list[str] = []
    cursor = None
    for _ in range(1000):
        query = dict(params)
        if cursor is not None:
            query["cursor"] = cursor
        resp = client.get("/items", params=query, headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        if "limit" in params:
            assert len(body["items"]) <= params["limit"]
        ids.extend(i["id"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return ids
    raise AssertionError("pagination did not terminate")


def test_search_items_pages_return_every_item_exactly_once(client, fake_db):
    expected = [
        fake_db.add_item(
            sku=f"SKU{i}",
            name=f"item {i}",
            warehouse_id="w1",
            quantity=1,
            tenant_id="tenant-a",
        )["id"]
        for i in range(7)
    ]
    expected.sort()
    for limit in (1, 2, 3, 6, 7, 8, 50):
        ids = _walk_pages(client, TENANT_A, limit=limit)
        assert ids == expected, f"limit={limit}"


def test_search_items_next_cursor_is_last_item_of_page(client, fake_db):
    for i in range(3):
        fake_db.add_item(
            sku=f"SKU{i}",
            name=f"item {i}",
            warehouse_id="w1",
            quantity=1,
            tenant_id="tenant-a",
        )
    body = client.get("/items", params={"limit": 2}, headers=TENANT_A).json()
    assert body["next_cursor"] == body["items"][-1]["id"]
    body2 = client.get(
        "/items", params={"limit": 2, "cursor": body["next_cursor"]}, headers=TENANT_A
    ).json()
    all_ids = sorted(r["id"] for r in fake_db.items)
    assert [i["id"] for i in body["items"]] == all_ids[:2]
    assert [i["id"] for i in body2["items"]] == all_ids[2:]
    assert body2["next_cursor"] is None


def test_search_items_exact_multiple_of_limit_has_no_trailing_empty_page(
    client, fake_db
):
    for i in range(4):
        fake_db.add_item(
            sku=f"SKU{i}",
            name=f"item {i}",
            warehouse_id="w1",
            quantity=1,
            tenant_id="tenant-a",
        )
    body = client.get("/items", params={"limit": 4}, headers=TENANT_A).json()
    assert len(body["items"]) == 4
    assert body["next_cursor"] is None


def test_search_items_pagination_respects_filters_and_tenant(client, fake_db):
    expected = []
    for i in range(12):
        warehouse = "w1" if i % 2 == 0 else "w2"
        name = f"bolt {i}" if i % 3 else f"nut {i}"
        tenant = "tenant-a" if i % 5 else "tenant-b"
        row = fake_db.add_item(
            sku=f"SKU{i}",
            name=name,
            warehouse_id=warehouse,
            quantity=1,
            tenant_id=tenant,
        )
        if warehouse == "w1" and "bolt" in name and tenant == "tenant-a":
            expected.append(row["id"])
    expected.sort()
    assert len(expected) >= 3
    for limit in (1, 2, 5):
        ids = _walk_pages(client, TENANT_A, limit=limit, warehouse_id="w1", q="bolt")
        assert ids == expected, f"limit={limit}"


def test_search_items_rejects_non_positive_limit(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    for limit in (0, -1):
        resp = client.get("/items", params={"limit": limit}, headers=TENANT_A)
        assert resp.status_code == 422
