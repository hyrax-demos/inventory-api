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


def _walk_pages(client, params, headers):
    """Follow next_cursor from the first page to the last; return all ids seen."""
    seen: list[str] = []
    cursor = None
    for _ in range(1000):  # guard against a cursor that never terminates
        page_params = dict(params)
        if cursor is not None:
            page_params["cursor"] = cursor
        resp = client.get("/items", params=page_params, headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) <= params["limit"]
        seen.extend(i["id"] for i in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return seen
    raise AssertionError("pagination did not terminate")


def _seed_mixed(fake_db, n=23):
    for i in range(n):
        fake_db.add_item(
            sku=f"SKU{i}",
            name="foo widget" if i % 2 else "bar gadget",
            warehouse_id="w1" if i % 3 else "w2",
            quantity=1,
            tenant_id="tenant-a" if i % 5 else "tenant-b",
        )


def _expected_ids(fake_db, tenant_id, warehouse_id="", q=""):
    rows = [
        r
        for r in fake_db.items
        if r["tenant_id"] == tenant_id
        and (not warehouse_id or r["warehouse_id"] == warehouse_id)
        and (not q or q.lower() in r["name"].lower())
    ]
    return sorted(r["id"] for r in rows)


def test_search_items_page_walk_returns_every_item_exactly_once(client, fake_db):
    for i in range(7):
        fake_db.add_item(sku=f"SKU{i}", name=f"item {i}", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    for limit in range(1, 9):
        seen = _walk_pages(client, {"limit": limit}, TENANT_A)
        assert seen == _expected_ids(fake_db, "tenant-a"), f"limit={limit}"


def test_search_items_page_boundary_has_no_duplicate(client, fake_db):
    for i in range(4):
        fake_db.add_item(sku=f"SKU{i}", name=f"item {i}", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    first = client.get("/items", params={"limit": 2}, headers=TENANT_A).json()
    second = client.get(
        "/items", params={"limit": 2, "cursor": first["next_cursor"]}, headers=TENANT_A
    ).json()
    assert first["items"][-1]["id"] != second["items"][0]["id"]
    assert first["items"][-1]["id"] < second["items"][0]["id"]
    assert second["next_cursor"] is None


def test_search_items_last_full_page_has_no_cursor(client, fake_db):
    # Exactly limit * k matches: the final page must not advertise an empty next page.
    for i in range(4):
        fake_db.add_item(sku=f"SKU{i}", name=f"item {i}", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    first = client.get("/items", params={"limit": 2}, headers=TENANT_A).json()
    second = client.get(
        "/items", params={"limit": 2, "cursor": first["next_cursor"]}, headers=TENANT_A
    ).json()
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None


def test_search_items_page_walk_with_filters(client, fake_db):
    _seed_mixed(fake_db)
    filter_sets = [
        {},
        {"warehouse_id": "w1"},
        {"q": "foo"},
        {"warehouse_id": "w1", "q": "FOO"},
    ]
    for filters in filter_sets:
        expected = _expected_ids(fake_db, "tenant-a", **filters)
        assert expected, filters
        for limit in (1, 2, 3, 5, len(expected), len(expected) + 1):
            seen = _walk_pages(client, {"limit": limit, **filters}, TENANT_A)
            assert seen == expected, f"limit={limit} filters={filters}"


def test_search_items_page_walk_is_tenant_scoped(client, fake_db):
    _seed_mixed(fake_db)
    ids_a = _walk_pages(client, {"limit": 2}, TENANT_A)
    ids_b = _walk_pages(client, {"limit": 2}, TENANT_B)
    assert ids_a == _expected_ids(fake_db, "tenant-a")
    assert ids_b == _expected_ids(fake_db, "tenant-b")
    assert set(ids_a).isdisjoint(ids_b)
