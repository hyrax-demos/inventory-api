"""Endpoint-level tests for POST /transfers and GET /transfers.

Requests go through the real FastAPI app, route handlers, service and repo.
Only ``app.db.transaction`` is replaced, by the in-memory ``Store`` from the
service tests. That store understands the transfer repo's SQL and really
rolls back: it snapshots state when the transaction opens and restores it
if the block raises. So these tests can check atomicity over HTTP.
"""

import base64
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import cache, db, transfers_repo
from app.main import app
from app.routes import items as items_routes
from tests.test_transfers_service import Store

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
HEADERS_A = {"X-Tenant-Id": TENANT_A}
HEADERS_B = {"X-Tenant-Id": TENANT_B}


@pytest.fixture
def store(monkeypatch):
    s = Store()
    monkeypatch.setattr(db, "transaction", s.transaction)
    # tenant-a: WIDGET at wA; wB exists (it holds another SKU) but has no
    # WIDGET row, so a transfer into it has to create the row.
    s.add_item(TENANT_A, "wA", "WIDGET", 10)
    s.add_item(TENANT_A, "wB", "GADGET", 1)
    # tenant-b owns separate warehouses with the same SKU.
    s.add_item(TENANT_B, "wX", "WIDGET", 50)
    s.add_item(TENANT_B, "wY", "GADGET", 5)
    return s


@pytest.fixture
def stock_reads(monkeypatch, store):
    """Serve GET /items/{sku}/stock from the same store the transfers use."""

    def fetch_one(sql, params=()):
        assert sql.startswith("SELECT quantity FROM items")
        sku, warehouse_id, tenant_id = params
        qty = store.qty(tenant_id, warehouse_id, sku)
        return None if qty is None else {"quantity": qty}

    monkeypatch.setattr(items_routes, "fetch_one", fetch_one)


@pytest.fixture
def invalidations(monkeypatch):
    calls: list[str] = []
    real = cache.invalidate

    def spy(key):
        calls.append(key)
        real(key)

    monkeypatch.setattr(cache, "invalidate", spy)
    return calls


def _body(**overrides):
    body = {
        "sku": "WIDGET",
        "source_warehouse_id": "wA",
        "destination_warehouse_id": "wB",
        "quantity": 4,
    }
    body.update(overrides)
    return body


def _snapshot(store):
    """Everything a rejected transfer must leave untouched."""
    return (
        sorted(
            (r["tenant_id"], r["warehouse_id"], r["sku"], r["quantity"])
            for r in store.items
        ),
        list(store.transfers),
        list(store.movements),
    )


def _assert_nothing_written(store, before, invalidations):
    assert _snapshot(store) == before
    assert store.transfers == []
    assert store.movements == []
    assert invalidations == []


# -- (1) happy path --


def test_transfer_creates_destination_row_and_moves_exact_quantity(
    client, store, invalidations
):
    assert store.qty(TENANT_A, "wB", "WIDGET") is None

    resp = client.post("/transfers", json=_body(), headers=HEADERS_A)

    assert resp.status_code == 201
    data = resp.json()
    assert data["sku"] == "WIDGET"
    assert data["source_warehouse_id"] == "wA"
    assert data["destination_warehouse_id"] == "wB"
    assert data["quantity"] == 4
    assert "tenant_id" not in data
    assert store.qty(TENANT_A, "wA", "WIDGET") == 6
    assert store.qty(TENANT_A, "wB", "WIDGET") == 4
    # Other rows are untouched.
    assert store.qty(TENANT_A, "wB", "GADGET") == 1
    assert store.qty(TENANT_B, "wX", "WIDGET") == 50

    assert len(store.transfers) == 1
    assert str(store.transfers[0]["id"]) == data["id"]
    assert store.transfers[0]["tenant_id"] == TENANT_A

    movements = [m for m in store.movements if m["transfer_id"] == data["id"]]
    assert len(movements) == 2 == len(store.movements)
    by_reason = {m["reason"]: m for m in movements}
    assert set(by_reason) == {"transfer_out", "transfer_in"}
    out, inn = by_reason["transfer_out"], by_reason["transfer_in"]
    assert (out["tenant_id"], out["warehouse_id"], out["sku"], out["delta"]) == (
        TENANT_A,
        "wA",
        "WIDGET",
        -4,
    )
    assert (inn["tenant_id"], inn["warehouse_id"], inn["sku"], inn["delta"]) == (
        TENANT_A,
        "wB",
        "WIDGET",
        4,
    )

    assert cache.stock_key("WIDGET") in invalidations


def test_transfer_into_existing_destination_row_adds_quantity(client, store):
    store.add_item(TENANT_A, "wB", "WIDGET", 3)

    resp = client.post("/transfers", json=_body(quantity=10), headers=HEADERS_A)

    assert resp.status_code == 201
    assert store.qty(TENANT_A, "wA", "WIDGET") == 0
    assert store.qty(TENANT_A, "wB", "WIDGET") == 13
    # Upsert must not create a duplicate destination row.
    assert (
        sum(
            1
            for r in store.items
            if (r["tenant_id"], r["warehouse_id"], r["sku"])
            == (TENANT_A, "wB", "WIDGET")
        )
        == 1
    )
    assert len(store.movements) == 2


def test_stock_read_after_transfer_is_fresh(client, store, stock_reads, invalidations):
    store.add_item(TENANT_A, "wB", "WIDGET", 3)
    # Warm the stock cache for both sides.
    src = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "wA"}, headers=HEADERS_A
    )
    assert src.json()["quantity"] == 10
    assert cache.get(cache.stock_key("WIDGET")) == 10

    resp = client.post("/transfers", json=_body(quantity=4), headers=HEADERS_A)
    assert resp.status_code == 201

    assert cache.get(cache.stock_key("WIDGET")) is None
    src = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "wA"}, headers=HEADERS_A
    )
    assert src.json()["quantity"] == 6
    cache.invalidate(cache.stock_key("WIDGET"))
    dst = client.get(
        "/items/WIDGET/stock", params={"warehouse_id": "wB"}, headers=HEADERS_A
    )
    assert dst.json()["quantity"] == 7


# -- (2) validation errors --


def _post_rejected(client, store, invalidations, body, status, code):
    before = _snapshot(store)
    resp = client.post("/transfers", json=body, headers=HEADERS_A)
    assert resp.status_code == status, resp.text
    assert resp.json()["code"] == code
    assert "detail" in resp.json()
    _assert_nothing_written(store, before, invalidations)
    return resp


def test_quantity_zero_is_rejected(client, store, invalidations):
    _post_rejected(
        client, store, invalidations, _body(quantity=0), 400, "invalid_quantity"
    )


def test_negative_quantity_is_rejected(client, store, invalidations):
    _post_rejected(
        client, store, invalidations, _body(quantity=-3), 400, "invalid_quantity"
    )


@pytest.mark.parametrize("quantity", [1.5, "4", True, None, [4]])
def test_non_integer_quantity_is_rejected(client, store, invalidations, quantity):
    _post_rejected(
        client,
        store,
        invalidations,
        _body(quantity=quantity),
        400,
        "invalid_quantity",
    )


def test_missing_quantity_is_rejected(client, store, invalidations):
    body = _body()
    del body["quantity"]
    _post_rejected(client, store, invalidations, body, 400, "invalid_quantity")


@pytest.mark.parametrize(
    "field", ["sku", "source_warehouse_id", "destination_warehouse_id"]
)
def test_missing_field_is_rejected(client, store, invalidations, field):
    body = _body()
    del body[field]
    _post_rejected(client, store, invalidations, body, 400, "invalid_field")


@pytest.mark.parametrize(
    "field", ["sku", "source_warehouse_id", "destination_warehouse_id"]
)
@pytest.mark.parametrize("value", ["", "   ", 7, None])
def test_blank_or_non_string_field_is_rejected(
    client, store, invalidations, field, value
):
    _post_rejected(
        client, store, invalidations, _body(**{field: value}), 400, "invalid_field"
    )


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b'"str"'])
def test_malformed_body_is_rejected(client, store, invalidations, raw):
    before = _snapshot(store)
    resp = client.post(
        "/transfers",
        content=raw,
        headers={**HEADERS_A, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_body"
    _assert_nothing_written(store, before, invalidations)


def test_same_source_and_destination_is_rejected(client, store, invalidations):
    _post_rejected(
        client,
        store,
        invalidations,
        _body(destination_warehouse_id="wA"),
        400,
        "same_warehouse",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_warehouse_id": "nope"},
        {"destination_warehouse_id": "nope"},
    ],
)
def test_unknown_warehouse_is_404(client, store, invalidations, overrides):
    _post_rejected(
        client,
        store,
        invalidations,
        _body(**overrides),
        404,
        "warehouse_not_found",
    )


def test_unknown_sku_is_404(client, store, invalidations):
    _post_rejected(
        client, store, invalidations, _body(sku="NOPE"), 404, "sku_not_found"
    )


def test_insufficient_stock_is_409(client, store, invalidations):
    _post_rejected(
        client,
        store,
        invalidations,
        _body(quantity=11),
        409,
        "insufficient_stock",
    )


def test_no_stock_row_at_source_is_409(client, store, invalidations):
    # wB exists for tenant-a but holds no WIDGET.
    _post_rejected(
        client,
        store,
        invalidations,
        _body(source_warehouse_id="wB", destination_warehouse_id="wA", quantity=1),
        409,
        "insufficient_stock",
    )


# -- (3) atomicity on failure --


@pytest.fixture
def server_error_client() -> TestClient:
    """Returns 500 responses instead of re-raising server exceptions."""
    return TestClient(app, raise_server_exceptions=False)


def _boom(*args, **kwargs):
    raise RuntimeError("simulated failure")


def _fail_second_movement(monkeypatch):
    real = transfers_repo.insert_movement
    calls = {"n": 0}

    def insert_movement(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(transfers_repo, "insert_movement", insert_movement)


@pytest.mark.parametrize(
    "failure",
    ["increment_stock", "insert_transfer", "first_movement", "second_movement"],
)
def test_failure_after_source_decrement_rolls_everything_back(
    monkeypatch, server_error_client, store, invalidations, failure
):
    cache.put(cache.stock_key("WIDGET"), 10)
    decrements = []
    real_decrement = transfers_repo.decrement_stock

    def decrement_spy(*args, **kwargs):
        ok = real_decrement(*args, **kwargs)
        decrements.append(ok)
        return ok

    monkeypatch.setattr(transfers_repo, "decrement_stock", decrement_spy)
    if failure == "second_movement":
        _fail_second_movement(monkeypatch)
    elif failure == "first_movement":
        monkeypatch.setattr(transfers_repo, "insert_movement", _boom)
    else:
        monkeypatch.setattr(transfers_repo, failure, _boom)
    before = _snapshot(store)

    resp = server_error_client.post("/transfers", json=_body(), headers=HEADERS_A)

    assert resp.status_code == 500
    # The source really was decremented inside the transaction before failing.
    assert decrements == [True]
    assert store.qty(TENANT_A, "wA", "WIDGET") == 10
    assert store.qty(TENANT_A, "wB", "WIDGET") is None
    _assert_nothing_written(store, before, invalidations)
    assert store.commits == 0 and store.rollbacks == 1
    # The cached value was not dropped because nothing committed.
    assert cache.get(cache.stock_key("WIDGET")) == 10


# -- (4) tenant isolation --


@pytest.mark.parametrize(
    "overrides",
    [
        # out of tenant-a's warehouse into tenant-b's own
        {"source_warehouse_id": "wA", "destination_warehouse_id": "wX"},
        # into tenant-a's warehouse from tenant-b's own
        {"source_warehouse_id": "wX", "destination_warehouse_id": "wA"},
        # between two of tenant-a's warehouses
        {"source_warehouse_id": "wA", "destination_warehouse_id": "wB"},
    ],
)
def test_tenant_b_cannot_touch_tenant_a_warehouses(
    client, store, invalidations, overrides
):
    before = _snapshot(store)
    resp = client.post(
        "/transfers", json=_body(quantity=1, **overrides), headers=HEADERS_B
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "warehouse_not_found"
    _assert_nothing_written(store, before, invalidations)


def test_tenant_b_list_never_includes_tenant_a_transfers(client, store):
    a_ids = set()
    for _ in range(3):
        resp = client.post("/transfers", json=_body(quantity=1), headers=HEADERS_A)
        assert resp.status_code == 201
        a_ids.add(resp.json()["id"])

    empty = client.get("/transfers", headers=HEADERS_B)
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None}

    resp = client.post(
        "/transfers",
        json=_body(source_warehouse_id="wX", destination_warehouse_id="wY", quantity=1),
        headers=HEADERS_B,
    )
    assert resp.status_code == 201
    b_id = resp.json()["id"]

    b_ids = set()
    cursor = None
    while True:
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/transfers", params=params, headers=HEADERS_B).json()
        b_ids.update(i["id"] for i in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert b_ids == {b_id}
    assert not (b_ids & a_ids)

    a_list = client.get("/transfers", headers=HEADERS_A).json()
    assert {i["id"] for i in a_list["items"]} == a_ids


def test_tenant_b_cannot_use_tenant_a_cursor_to_read_a_rows(client, store):
    for _ in range(3):
        client.post("/transfers", json=_body(quantity=1), headers=HEADERS_A)
    a_page = client.get("/transfers", params={"limit": 1}, headers=HEADERS_A)
    cursor = a_page.json()["next_cursor"]
    assert cursor is not None

    resp = client.get("/transfers", params={"cursor": cursor}, headers=HEADERS_B)
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


# -- (5) GET pagination --


def _create_many(client, n):
    ids = []
    for _ in range(n):
        resp = client.post("/transfers", json=_body(quantity=1), headers=HEADERS_A)
        assert resp.status_code == 201
        ids.append(resp.json()["id"])
    return ids


def _walk(client, limit):
    pages = []
    cursor = None
    while True:
        params = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        resp = client.get("/transfers", params=params, headers=HEADERS_A)
        assert resp.status_code == 200
        page = resp.json()
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 50, "pagination did not terminate"


def test_pagination_newest_first_without_gaps_or_duplicates(client, store):
    created = _create_many(client, 7)
    newest_first = list(reversed(created))

    pages = _walk(client, 3)

    assert [len(p["items"]) for p in pages] == [3, 3, 1]
    assert all(p["next_cursor"] for p in pages[:-1])
    assert pages[-1]["next_cursor"] is None
    seen = [i["id"] for p in pages for i in p["items"]]
    assert seen == newest_first
    assert len(set(seen)) == len(seen)
    stamps = [i["created_at"] for p in pages for i in p["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_default_limit_is_20(client, store):
    store.add_item(TENANT_A, "wA", "BULK", 1000)
    for _ in range(21):
        client.post("/transfers", json=_body(sku="BULK", quantity=1), headers=HEADERS_A)
    first = client.get("/transfers", headers=HEADERS_A).json()
    assert len(first["items"]) == 20
    assert first["next_cursor"] is not None
    rest = client.get(
        "/transfers", params={"cursor": first["next_cursor"]}, headers=HEADERS_A
    ).json()
    assert len(rest["items"]) == 1
    assert rest["next_cursor"] is None


def test_exact_final_page_has_null_cursor(client, store):
    _create_many(client, 4)
    pages = _walk(client, 2)
    assert [len(p["items"]) for p in pages] == [2, 2]
    assert pages[-1]["next_cursor"] is None


def test_max_limit_100_is_accepted(client, store):
    _create_many(client, 2)
    resp = client.get("/transfers", params={"limit": 100}, headers=HEADERS_A)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2
    assert resp.json()["next_cursor"] is None


def test_pagination_breaks_created_at_ties_by_id(client, store):
    """Rows sharing a created_at must still page with no gaps or repeats."""
    same_time = datetime(2024, 6, 1, tzinfo=timezone.utc)
    ids = [uuid.uuid4() for _ in range(5)]
    for tid in ids:
        store.transfers.append(
            {
                "id": tid,
                "tenant_id": TENANT_A,
                "sku": "WIDGET",
                "source_warehouse_id": "wA",
                "destination_warehouse_id": "wB",
                "quantity": 1,
                "created_at": same_time,
            }
        )
    expected = [str(t) for t in sorted(ids, reverse=True)]

    pages = _walk(client, 2)

    assert [i["id"] for p in pages for i in p["items"]] == expected


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", "1.5", ""])
def test_invalid_limit_is_400(client, store, limit):
    resp = client.get("/transfers", params={"limit": limit}, headers=HEADERS_A)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_limit"


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage!!",
        "Zm9v",  # base64 of "foo": no separator
        base64.urlsafe_b64encode(b"not-a-date|x").decode(),
        base64.urlsafe_b64encode(b"2024-01-01T00:00:00+00:00|not-a-uuid").decode(),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
    ],
)
def test_garbage_cursor_is_400(client, store, cursor):
    resp = client.get("/transfers", params={"cursor": cursor}, headers=HEADERS_A)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_cursor"
