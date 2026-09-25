"""Shared test fixtures for the inventory-api test suite.

Everything runs in-process against a FakeDB: no Postgres, no network, no
real secrets. Environment variables the app reads at import time are set
before ``app.main`` is imported for the first time.
"""

import re
import os
from contextlib import contextmanager

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("DB_PASSWORD", "unused-in-tests")
os.environ.setdefault("WAREHOUSE_API_KEY", "unused-in-tests")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import cache as cache_module  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import admin as admin_routes  # noqa: E402
from app.routes import items as items_routes  # noqa: E402
from app.routes import reports as reports_routes  # noqa: E402
from app.routes import sync as sync_routes  # noqa: E402

ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
ADMIN_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


class FakeDB:
    """A tiny stand-in for ``app.db``'s fetch_all / fetch_one / execute.

    It dispatches on the literal SQL text each route passes (every query in
    this app is a fixed, known shape) and runs the equivalent operation
    against plain in-memory lists, instead of talking to Postgres. It is
    intentionally not a general SQL engine -- just enough to drive every
    route's happy path and its documented branch conditions.
    """

    def __init__(self):
        self.items: list[dict] = []
        self.reservations: list[dict] = []
        self.movements: list[dict] = []
        self._next_id = 1

    # -- seeding helpers used by tests --
    def add_item(self, **kw) -> dict:
        row = {"id": str(self._next_id), "name": "item", "price": 0.0, **kw}
        self._next_id += 1
        self.items.append(row)
        return row

    def add_reservation(self, **kw) -> dict:
        row = dict(kw)
        self.reservations.append(row)
        return row

    def add_movement(self, **kw) -> dict:
        row = dict(kw)
        self.movements.append(row)
        return row

    # -- fetch_all --
    def fetch_all(self, sql: str, params: tuple = ()):
        params = list(params)
        if "ORDER BY id ASC LIMIT" in sql:
            return self._search_items(sql, params)
        if "quantity <= %s" in sql:
            tenant_id, threshold = params
            rows = [
                {
                    "sku": r["sku"],
                    "name": r["name"],
                    "warehouse_id": r["warehouse_id"],
                    "quantity": r["quantity"],
                }
                for r in self.items
                if r["tenant_id"] == tenant_id and r["quantity"] <= threshold
            ]
            rows.sort(key=lambda r: r["quantity"])
            return rows
        if "FROM movements" in sql:
            tenant_id, start_of_day = params
            cutoff = _as_naive(start_of_day)
            rows = [
                dict(m)
                for m in self.movements
                if m["tenant_id"] == tenant_id and _as_naive(m["created_at"]) >= cutoff
            ]
            rows.sort(key=lambda r: r["created_at"])
            return rows
        if sql.startswith("SELECT sku, warehouse_id, quantity FROM reservations"):
            row = self.fetch_one(sql, tuple(params))
            return [row] if row else []
        if "JOIN items i" in sql:
            (tenant_id,) = params
            # Whether the join itself constrains the item row by tenant.
            # Baseline SQL mentions "tenant_id" exactly once (in the WHERE
            # on reservations); any fix that scopes the join to the same
            # tenant adds a second mention, however it spells the alias.
            tenant_scoped_join = sql.count("tenant_id") > 1
            lines: dict[tuple, dict] = {}
            for r in self.reservations:
                if r["tenant_id"] != tenant_id:
                    continue
                item = next(
                    (
                        i
                        for i in self.items
                        if i["sku"] == r["sku"]
                        and i["warehouse_id"] == r["warehouse_id"]
                        and (not tenant_scoped_join or i["tenant_id"] == tenant_id)
                    ),
                    None,
                )
                if item is None:
                    continue
                key = (r["sku"], r["warehouse_id"])
                agg = lines.setdefault(
                    key,
                    {
                        "sku": r["sku"],
                        "warehouse_id": r["warehouse_id"],
                        "reserved_qty": 0,
                        "reserved_value": 0.0,
                    },
                )
                agg["reserved_qty"] += r["quantity"]
                agg["reserved_value"] += r["quantity"] * item["price"]
            out = list(lines.values())
            out.sort(key=lambda r: r["reserved_value"], reverse=True)
            return out
        raise AssertionError(f"FakeDB.fetch_all: unrecognized query: {sql!r}")

    def _search_items(self, sql: str, params: list):
        params = list(params)
        tenant_id = params.pop(0)
        limit_plus1 = params.pop(-1)
        warehouse_id = params.pop(0) if "warehouse_id = %s" in sql else None
        name_like = params.pop(0) if "name ILIKE %s" in sql else None
        cursor = params.pop(0) if "id >= %s" in sql else None
        rows = [r for r in self.items if r["tenant_id"] == tenant_id]
        if warehouse_id is not None:
            rows = [r for r in rows if r["warehouse_id"] == warehouse_id]
        if name_like is not None:
            needle = name_like.strip("%").lower()
            rows = [r for r in rows if needle in r["name"].lower()]
        if cursor is not None:
            rows = [r for r in rows if r["id"] >= cursor]
        rows.sort(key=lambda r: r["id"])
        return [dict(r) for r in rows[:limit_plus1]]

    # -- fetch_one --
    def fetch_one(self, sql: str, params: tuple = ()):
        params = list(params)
        if sql.startswith("SELECT * FROM items WHERE sku = %s AND tenant_id = %s"):
            sku, tenant_id = params
            return next(
                (
                    dict(r)
                    for r in self.items
                    if r["sku"] == sku and r["tenant_id"] == tenant_id
                ),
                None,
            )
        if sql.startswith("SELECT quantity FROM items"):
            sku, warehouse_id, tenant_id = params
            row = next(
                (
                    r
                    for r in self.items
                    if r["sku"] == sku
                    and r["warehouse_id"] == warehouse_id
                    and r["tenant_id"] == tenant_id
                ),
                None,
            )
            return {"quantity": row["quantity"]} if row else None
        if sql.startswith("SELECT 1 FROM reservations"):
            order_id, tenant_id = params
            row = next(
                (
                    r
                    for r in self.reservations
                    if r["order_id"] == order_id and r["tenant_id"] == tenant_id
                ),
                None,
            )
            return {"?column?": 1} if row else None
        if sql.startswith("SELECT sku, warehouse_id, quantity FROM reservations"):
            order_id, tenant_id = params
            row = next(
                (
                    r
                    for r in self.reservations
                    if r["order_id"] == order_id and r["tenant_id"] == tenant_id
                ),
                None,
            )
            return dict(row) if row else None
        raise AssertionError(f"FakeDB.fetch_one: unrecognized query: {sql!r}")

    # -- execute --
    def execute(self, sql: str, params: tuple = ()):
        params = list(params)

        if sql.startswith("UPDATE items SET quantity = quantity - %s"):
            delta, sku, warehouse_id, tenant_id = params
            return self._update_items(
                lambda r: (
                    r["sku"] == sku
                    and r["warehouse_id"] == warehouse_id
                    and r["tenant_id"] == tenant_id
                ),
                lambda r: r.__setitem__("quantity", r["quantity"] - delta),
            )

        if sql.startswith("INSERT INTO reservations"):
            order_id, tenant_id, sku, warehouse_id, quantity = params
            self.reservations.append(
                {
                    "order_id": order_id,
                    "tenant_id": tenant_id,
                    "sku": sku,
                    "warehouse_id": warehouse_id,
                    "quantity": quantity,
                }
            )
            return 1

        if (
            sql.startswith("UPDATE items SET")
            and "WHERE id = %s AND tenant_id = %s" in sql
        ):
            *values, item_id, tenant_id = params
            cols = re.findall(r"(\w+) = %s", sql.split("WHERE")[0])
            return self._update_items(
                lambda r: r["id"] == item_id and r["tenant_id"] == tenant_id,
                lambda r: r.update(dict(zip(cols, values))),
            )

        if sql.startswith("UPDATE items SET quantity = %s"):
            quantity, sku, warehouse_id, tenant_id = params
            return self._update_items(
                lambda r: (
                    r["sku"] == sku
                    and r["warehouse_id"] == warehouse_id
                    and r["tenant_id"] == tenant_id
                ),
                lambda r: r.__setitem__("quantity", quantity),
            )

        if sql.startswith("UPDATE items SET price = %s"):
            price, sku, warehouse_id, tenant_id = params
            return self._update_items(
                lambda r: (
                    r["sku"] == sku
                    and r["warehouse_id"] == warehouse_id
                    and r["tenant_id"] == tenant_id
                ),
                lambda r: r.__setitem__("price", price),
            )

        if (
            sql.startswith("UPDATE items SET quantity = quantity + %s")
            and "warehouse_id = %s" in sql
        ):
            delta, sku, warehouse_id, tenant_id = params
            return self._update_items(
                lambda r: (
                    r["sku"] == sku
                    and r["warehouse_id"] == warehouse_id
                    and r["tenant_id"] == tenant_id
                ),
                lambda r: r.__setitem__("quantity", r["quantity"] + delta),
            )

        if sql.startswith("UPDATE items SET quantity = quantity + %s"):
            delta, sku, tenant_id = params
            return self._update_items(
                lambda r: r["sku"] == sku and r["tenant_id"] == tenant_id,
                lambda r: r.__setitem__("quantity", r["quantity"] + delta),
            )

        if sql.startswith("DELETE FROM reservations"):
            order_id, tenant_id = params
            before = len(self.reservations)
            self.reservations = [
                r
                for r in self.reservations
                if not (r["order_id"] == order_id and r["tenant_id"] == tenant_id)
            ]
            return before - len(self.reservations)

        if sql.startswith("UPDATE items SET quantity = 0"):
            (tenant_id,) = params
            return self._update_items(
                lambda r: r["tenant_id"] == tenant_id,
                lambda r: r.__setitem__("quantity", 0),
            )

        if sql.startswith("DELETE FROM items"):
            item_id, tenant_id = params
            before = len(self.items)
            self.items = [
                r
                for r in self.items
                if not (r["id"] == item_id and r["tenant_id"] == tenant_id)
            ]
            return before - len(self.items)

        raise AssertionError(f"FakeDB.execute: unrecognized query: {sql!r}")

    def _update_items(self, predicate, mutate) -> int:
        n = 0
        for r in self.items:
            if predicate(r):
                mutate(r)
                n += 1
        return n

    # -- transaction --
    # A stand-in for ``app.db.transaction()``, for a fix that wraps more than
    # one statement in a single transaction (e.g. an atomic release or an
    # all-or-nothing import). It has no real rollback semantics -- each
    # statement lands on the same in-memory store ``execute`` already uses --
    # so it exists only so route code that does
    # ``with transaction() as conn: conn.cursor().execute(...)`` has
    # something to call. Atomicity itself is graded by a lower-layer fake
    # connection in the oracle for the tasks that need it, per the bench
    # brief's "fake at the lowest layer" rule.
    @contextmanager
    def transaction(self):
        yield _FakeConnection(self)


class _FakeConnection:
    def __init__(self, fake_db: "FakeDB"):
        self._fake_db = fake_db

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._fake_db)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeCursor:
    def __init__(self, fake_db: "FakeDB"):
        self._fake_db = fake_db
        self._rowcount = 0
        self._rows: list = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            self._rows = self._fake_db.fetch_all(sql, params)
        else:
            self._rowcount = self._fake_db.execute(sql, params)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def close(self) -> None:
        pass


def _as_naive(dt):
    """Normalize a datetime for comparison inside the fake, regardless of
    whether the caller passes a naive or a tz-aware value."""
    if dt.tzinfo is not None:
        import datetime as _dt

        return dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return dt


@pytest.fixture(autouse=True)
def _clear_cache():
    cache_module._store.clear()
    yield
    cache_module._store.clear()


@pytest.fixture
def fake_db(monkeypatch) -> FakeDB:
    fake = FakeDB()
    # raising=False throughout: a reference fix for one of the bench tasks
    # is free to add or drop which of fetch_all/fetch_one/execute/transaction
    # a given route module imports from app.db (e.g. swapping `execute` for
    # `transaction` to wrap several statements atomically), and this fixture
    # must still patch whichever names end up bound there rather than
    # asserting today's exact import list.
    for module in (items_routes, reports_routes, sync_routes, admin_routes):
        monkeypatch.setattr(module, "fetch_all", fake.fetch_all, raising=False)
        monkeypatch.setattr(module, "fetch_one", fake.fetch_one, raising=False)
        monkeypatch.setattr(module, "execute", fake.execute, raising=False)
        monkeypatch.setattr(module, "transaction", fake.transaction, raising=False)
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
