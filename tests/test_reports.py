from datetime import datetime

TENANT_A = {"X-Tenant-Id": "tenant-a"}


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


def test_low_stock_report_rejects_empty_tenant_header(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400
    assert resp.json() == {"detail": "missing tenant"}


def test_low_stock_report_rejects_missing_tenant_header(client, fake_db):
    resp = client.get("/reports/low-stock")
    # Mirrors items.py: an absent required header is rejected by FastAPI.
    assert resp.status_code in (400, 422)


def test_low_stock_report_accepts_non_empty_tenant_header(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", headers=TENANT_A)
    assert resp.status_code == 200
    assert [i["sku"] for i in resp.json()["items"]] == ["A"]


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


def test_reserved_value_prices_only_against_same_tenant_item(client, fake_db):
    # tenant-b's item at the same SKU+warehouse is seeded first, so an
    # unscoped join would pick it up and price with its (much higher) price.
    fake_db.add_item(
        sku="WIDGET",
        warehouse_id="w1",
        quantity=100,
        price=1000.0,
        tenant_id="tenant-b",
    )
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
    assert line["reserved_value"] == 6.0


def test_reserved_value_skips_reservation_with_only_other_tenant_item(client, fake_db):
    fake_db.add_item(
        sku="WIDGET",
        warehouse_id="w1",
        quantity=100,
        price=1000.0,
        tenant_id="tenant-b",
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json()["lines"] == []


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


def _seed_import_items(fake_db):
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
    fake_db.add_item(sku="C", warehouse_id="w1", quantity=3, tenant_id="tenant-a")


def _quantities(fake_db):
    return {r["sku"]: r["quantity"] for r in fake_db.items}


def test_import_snapshot_malformed_later_entry_applies_nothing(client, fake_db):
    _seed_import_items(fake_db)
    before = _quantities(fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": "not-a-number"},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 400
    assert resp.json() == {"detail": "malformed snapshot entry"}
    assert _quantities(fake_db) == before


def test_import_snapshot_malformed_entry_anywhere_applies_nothing(client, fake_db):
    _seed_import_items(fake_db)
    before = _quantities(fake_db)
    for bad in ({"sku": "B", "warehouse_id": "w1"}, "garbage", None):
        resp = client.post(
            "/reports/import",
            json={
                "items": [
                    {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                    bad,
                    {"sku": "C", "warehouse_id": "w1", "quantity": 30},
                ]
            },
            headers=TENANT_A,
        )
        assert resp.status_code == 400
        assert _quantities(fake_db) == before


class _FailingWriteError(Exception):
    pass


class _BufferingConnection:
    """A psycopg2-like connection over the FakeDB with real rollback:
    writes are buffered and only land on the store at ``commit()``."""

    def __init__(self, fake_db, fail_on_statement):
        self._fake_db = fake_db
        self._fail_on = fail_on_statement
        self._pending = []
        self._seen = 0

    def cursor(self, cursor_factory=None):
        return self

    def execute(self, sql, params=()):
        self._seen += 1
        if self._seen == self._fail_on:
            raise _FailingWriteError("simulated DB failure")
        self._pending.append((sql, params))

    def commit(self):
        for sql, params in self._pending:
            self._fake_db.execute(sql, params)
        self._pending = []

    def rollback(self):
        self._pending = []

    def close(self):
        pass


def _use_real_transaction(monkeypatch, fake_db, fail_on_statement=None):
    from app import db as db_module
    from app.routes import reports as reports_routes

    monkeypatch.setattr(
        db_module,
        "get_connection",
        lambda: _BufferingConnection(fake_db, fail_on_statement),
    )
    monkeypatch.setattr(reports_routes, "transaction", db_module.transaction)


def test_import_snapshot_write_failure_rolls_back_all(monkeypatch, fake_db):
    from fastapi.testclient import TestClient

    from app.main import app

    _seed_import_items(fake_db)
    before = _quantities(fake_db)
    _use_real_transaction(monkeypatch, fake_db, fail_on_statement=3)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
                {"sku": "C", "warehouse_id": "w1", "quantity": 30},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 500
    assert _quantities(fake_db) == before


def test_import_snapshot_valid_snapshot_applies_all_in_one_transaction(
    client, monkeypatch, fake_db
):
    _seed_import_items(fake_db)
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=7, tenant_id="tenant-b")
    _use_real_transaction(monkeypatch, fake_db)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": "20"},
                {"sku": "C", "warehouse_id": "w1", "quantity": 30},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": 3, "snapshot": '{"received": 3}'}
    by_tenant = {(r["sku"], r["tenant_id"]): r["quantity"] for r in fake_db.items}
    assert by_tenant == {
        ("A", "tenant-a"): 10,
        ("B", "tenant-a"): 20,
        ("C", "tenant-a"): 30,
        ("A", "tenant-b"): 7,
    }
