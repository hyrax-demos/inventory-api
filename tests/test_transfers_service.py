"""Service-level tests for app.transfers_service.

``app.db.transaction`` is replaced by an in-memory store that understands the
transfer repo's SQL. It has real rollback: state is snapshotted when the
transaction opens and restored if the block raises. That lets these tests
check atomicity and that the cache is only invalidated after a commit.
"""

import copy
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app import cache, db, transfers_service
from app.errors import ConflictError, NotFoundError, ValidationError


class Store:
    def __init__(self):
        self.items: list[dict] = []
        self.transfers: list[dict] = []
        self.movements: list[dict] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on: str | None = None  # SQL prefix that should raise
        self._clock = datetime(2024, 1, 1, tzinfo=timezone.utc)

    def add_item(self, tenant_id, warehouse_id, sku, quantity):
        self.items.append(
            {
                "tenant_id": tenant_id,
                "warehouse_id": warehouse_id,
                "sku": sku,
                "name": sku,
                "price": 0,
                "quantity": quantity,
            }
        )

    def qty(self, tenant_id, warehouse_id, sku):
        for r in self.items:
            if (r["tenant_id"], r["warehouse_id"], r["sku"]) == (
                tenant_id,
                warehouse_id,
                sku,
            ):
                return r["quantity"]
        return None

    def _snapshot(self):
        return copy.deepcopy((self.items, self.transfers, self.movements))

    def _restore(self, snap):
        self.items, self.transfers, self.movements = snap

    @contextmanager
    def transaction(self):
        snap = self._snapshot()
        try:
            yield FakeConn(self)
        except Exception:
            self._restore(snap)
            self.rollbacks += 1
            raise
        self.commits += 1

    # Executes one statement and returns (rowcount, rows).
    def run(self, sql, p):
        if self.fail_on and sql.startswith(self.fail_on):
            raise RuntimeError("simulated DB failure")
        if sql.startswith("SELECT 1 FROM items WHERE tenant_id = %s AND warehouse_id"):
            t, w = p
            hit = any(
                r["tenant_id"] == t and r["warehouse_id"] == w for r in self.items
            )
            return 0, [(1,)] if hit else []
        if sql.startswith("SELECT 1 FROM items WHERE tenant_id = %s AND sku"):
            t, s = p
            hit = any(r["tenant_id"] == t and r["sku"] == s for r in self.items)
            return 0, [(1,)] if hit else []
        if sql.startswith("UPDATE items SET quantity = quantity - %s"):
            n, t, w, s, n2 = p
            for r in self.items:
                if (r["tenant_id"], r["warehouse_id"], r["sku"]) == (t, w, s) and r[
                    "quantity"
                ] >= n2:
                    r["quantity"] -= n
                    return 1, []
            return 0, []
        if sql.startswith("INSERT INTO items"):
            t, w, s = p[:3]
            n = p[-1]
            for r in self.items:
                if (r["tenant_id"], r["warehouse_id"], r["sku"]) == (t, w, s):
                    r["quantity"] += n
                    return 1, []
            self.add_item(t, w, s, n)
            return 1, []
        if sql.startswith("INSERT INTO transfers"):
            t, s, src, dst, n = p
            self._clock += timedelta(seconds=1)
            row = {
                "id": uuid.uuid4(),
                "tenant_id": t,
                "sku": s,
                "source_warehouse_id": src,
                "destination_warehouse_id": dst,
                "quantity": n,
                "created_at": self._clock,
            }
            self.transfers.append(row)
            return 1, [dict(row)]
        if sql.startswith("INSERT INTO movements"):
            t, s, w, d, reason, tid = p
            self.movements.append(
                {
                    "tenant_id": t,
                    "sku": s,
                    "warehouse_id": w,
                    "delta": d,
                    "reason": reason,
                    "transfer_id": tid,
                }
            )
            return 1, []
        if sql.startswith("SELECT id, tenant_id, sku"):
            if "(created_at, id) <" in sql:
                t, ca, cid, limit = p
                key = (ca, uuid.UUID(cid))
            else:
                t, limit = p
                key = None
            rows = [r for r in self.transfers if r["tenant_id"] == t]
            rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
            if key is not None:
                rows = [r for r in rows if (r["created_at"], r["id"]) < key]
            return 0, [dict(r) for r in rows[:limit]]
        raise AssertionError(f"unexpected SQL: {sql!r}")


class FakeCursor:
    def __init__(self, store):
        self._store = store
        self.rowcount = 0
        self._rows = []

    def execute(self, sql, params=()):
        self.rowcount, self._rows = self._store.run(sql, tuple(params))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, store):
        self._store = store

    def cursor(self, cursor_factory=None):
        return FakeCursor(self._store)


@pytest.fixture
def store(monkeypatch):
    s = Store()
    monkeypatch.setattr(db, "transaction", s.transaction)
    return s


@pytest.fixture
def invalidations(monkeypatch):
    calls: list[str] = []
    real = cache.invalidate

    def spy(key):
        calls.append(key)
        real(key)

    monkeypatch.setattr(cache, "invalidate", spy)
    return calls


def _seed(store):
    store.add_item("tenant-a", "w1", "WIDGET", 10)
    store.add_item("tenant-a", "w2", "GADGET", 1)
    store.add_item("tenant-b", "w9", "WIDGET", 50)


# -- create_transfer --


def test_create_transfer_happy_path(store, invalidations):
    _seed(store)
    cache.put(cache.stock_key("WIDGET"), 10)

    t = transfers_service.create_transfer("tenant-a", "WIDGET", "w1", "w2", 4)

    assert t.quantity == 4 and t.source_warehouse_id == "w1"
    assert store.qty("tenant-a", "w1", "WIDGET") == 6
    assert store.qty("tenant-a", "w2", "WIDGET") == 4  # destination row upserted
    assert len(store.transfers) == 1
    out, inn = store.movements
    assert (out["warehouse_id"], out["delta"], out["reason"]) == (
        "w1",
        -4,
        "transfer_out",
    )
    assert (inn["warehouse_id"], inn["delta"], inn["reason"]) == (
        "w2",
        4,
        "transfer_in",
    )
    assert out["transfer_id"] == inn["transfer_id"] == t.id
    assert store.commits == 1
    assert cache.stock_key("WIDGET") in invalidations
    assert cache.get(cache.stock_key("WIDGET")) is None


@pytest.mark.parametrize("qty", [0, -1, 1.5, "3", True, None])
def test_create_transfer_rejects_bad_quantity(store, invalidations, qty):
    _seed(store)
    with pytest.raises(ValidationError) as exc:
        transfers_service.create_transfer("tenant-a", "WIDGET", "w1", "w2", qty)
    assert exc.value.code == "invalid_quantity"
    assert exc.value.status_code == 400
    assert invalidations == []


def test_create_transfer_rejects_same_warehouse(store, invalidations):
    _seed(store)
    with pytest.raises(ValidationError) as exc:
        transfers_service.create_transfer("tenant-a", "WIDGET", "w1", "w1", 1)
    assert exc.value.code == "same_warehouse"
    assert invalidations == []


def test_quantity_is_validated_before_same_warehouse(store):
    with pytest.raises(ValidationError) as exc:
        transfers_service.create_transfer("tenant-a", "WIDGET", "w1", "w1", 0)
    assert exc.value.code == "invalid_quantity"


@pytest.mark.parametrize(
    "sku,src,dst,code",
    [
        ("WIDGET", "nope", "w2", "warehouse_not_found"),
        ("WIDGET", "w1", "nope", "warehouse_not_found"),
        ("NOPE", "w1", "w2", "sku_not_found"),
        # another tenant's warehouse looks exactly like a missing one
        ("WIDGET", "w9", "w1", "warehouse_not_found"),
        ("WIDGET", "w1", "w9", "warehouse_not_found"),
    ],
)
def test_create_transfer_not_found(store, invalidations, sku, src, dst, code):
    _seed(store)
    with pytest.raises(NotFoundError) as exc:
        transfers_service.create_transfer("tenant-a", sku, src, dst, 1)
    assert exc.value.code == code
    assert exc.value.status_code == 404
    assert store.transfers == [] and store.movements == []
    assert store.qty("tenant-b", "w9", "WIDGET") == 50
    assert invalidations == []


def test_insufficient_stock_leaves_both_rows_unchanged(store, invalidations):
    _seed(store)
    store.add_item("tenant-a", "w2", "WIDGET", 3)
    with pytest.raises(ConflictError) as exc:
        transfers_service.create_transfer("tenant-a", "WIDGET", "w1", "w2", 11)
    assert exc.value.code == "insufficient_stock"
    assert exc.value.status_code == 409
    assert store.qty("tenant-a", "w1", "WIDGET") == 10
    assert store.qty("tenant-a", "w2", "WIDGET") == 3
    assert store.transfers == [] and store.movements == []
    assert store.commits == 0 and store.rollbacks == 1
    assert invalidations == []


def test_no_source_stock_row_is_insufficient_stock(store, invalidations):
    _seed(store)  # w2 exists for tenant-a but has no WIDGET row
    with pytest.raises(ConflictError) as exc:
        transfers_service.create_transfer("tenant-a", "WIDGET", "w2", "w1", 1)
    assert exc.value.code == "insufficient_stock"
    assert store.qty("tenant-a", "w1", "WIDGET") == 10
    assert invalidations == []


@pytest.mark.parametrize(
    "failing_sql",
    ["INSERT INTO items", "INSERT INTO transfers", "INSERT INTO movements"],
)
def test_failure_mid_transaction_rolls_back_everything(
    store, invalidations, failing_sql
):
    _seed(store)
    store.fail_on = failing_sql
    with pytest.raises(RuntimeError):
        transfers_service.create_transfer("tenant-a", "WIDGET", "w1", "w2", 4)
    assert store.qty("tenant-a", "w1", "WIDGET") == 10
    assert store.qty("tenant-a", "w2", "WIDGET") is None
    assert store.transfers == [] and store.movements == []
    assert store.commits == 0
    assert invalidations == []


# -- list_transfers --


def _make_transfers(store, n, tenant="tenant-a"):
    store.add_item(tenant, "w1", "WIDGET", 1000)
    store.add_item(tenant, "w2", "WIDGET", 0)
    return [
        transfers_service.create_transfer(tenant, "WIDGET", "w1", "w2", 1)
        for _ in range(n)
    ]


def test_list_transfers_newest_first_and_paginates(store):
    created = _make_transfers(store, 5)
    expected = [str(t.id) for t in reversed(created)]

    page1 = transfers_service.list_transfers("tenant-a", 2)
    assert [i["id"] for i in page1.items] == expected[:2]
    assert page1.next_cursor is not None

    page2 = transfers_service.list_transfers("tenant-a", 2, page1.next_cursor)
    assert [i["id"] for i in page2.items] == expected[2:4]

    page3 = transfers_service.list_transfers("tenant-a", 2, page2.next_cursor)
    assert [i["id"] for i in page3.items] == expected[4:]
    assert page3.next_cursor is None


def test_list_transfers_exact_page_has_null_cursor(store):
    _make_transfers(store, 3)
    page = transfers_service.list_transfers("tenant-a", 3)
    assert len(page.items) == 3
    assert page.next_cursor is None


def test_list_transfers_default_limit(store):
    _make_transfers(store, 21)
    page = transfers_service.list_transfers("tenant-a")
    assert len(page.items) == 20
    assert page.next_cursor is not None


def test_list_transfers_tenant_isolation(store):
    _make_transfers(store, 2, tenant="tenant-a")
    _make_transfers(store, 3, tenant="tenant-b")
    page = transfers_service.list_transfers("tenant-b", 10)
    assert len(page.items) == 3
    assert {i["tenant_id"] for i in page.items} == {"tenant-b"}


@pytest.mark.parametrize("limit", [0, 101, -5, "abc", 1.5, True])
def test_list_transfers_rejects_bad_limit(store, limit):
    with pytest.raises(ValidationError) as exc:
        transfers_service.list_transfers("tenant-a", limit)
    assert exc.value.code == "invalid_limit"


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!!",
        "Zm9v",  # "foo": no separator
        transfers_service.encode_cursor(
            datetime(2024, 1, 1, tzinfo=timezone.utc), "not-a-uuid"
        ),
        "bm90LWEtZGF0ZXx4",  # "not-a-date|x"
    ],
)
def test_list_transfers_rejects_malformed_cursor(store, cursor):
    with pytest.raises(ValidationError) as exc:
        transfers_service.list_transfers("tenant-a", 5, cursor)
    assert exc.value.code == "invalid_cursor"


def test_cursor_round_trip():
    ts = datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=timezone.utc)
    tid = str(uuid.uuid4())
    assert transfers_service.decode_cursor(
        transfers_service.encode_cursor(ts, tid)
    ) == (ts, tid)
