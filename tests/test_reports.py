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
    # Seed a row with an empty tenant_id so a missing check would leak it.
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="")
    resp = client.get("/reports/low-stock", headers={"X-Tenant-Id": ""})
    assert resp.status_code == 400
    assert resp.json() == {"detail": "missing tenant"}


def test_low_stock_report_valid_tenant_header_still_ok(client, fake_db):
    fake_db.add_item(
        sku="A", name="a", warehouse_id="w1", quantity=3, tenant_id="tenant-a"
    )
    fake_db.add_item(
        sku="B", name="b", warehouse_id="w1", quantity=1, tenant_id="tenant-a"
    )
    resp = client.get("/reports/low-stock", params={"threshold": 5}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["threshold"] == 5
    assert [i["sku"] for i in body["items"]] == ["B", "A"]


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


def test_reserved_value_prices_with_own_tenant_item_only(client, fake_db):
    # Tenant B's item comes first so an unscoped join would pick B's price.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=99.0, tenant_id="tenant-b"
    )
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert len(lines) == 1
    line = lines[0]
    assert line["sku"] == "WIDGET"
    assert line["warehouse_id"] == "w1"
    assert line["reserved_qty"] == 3
    assert line["reserved_value"] == 3 * 2.0


def test_reserved_value_not_priced_from_other_tenant_item(client, fake_db):
    # Only tenant B has an item at this sku/warehouse; A's reservation must
    # not be matched to it. With an inner join and no matching item, the
    # reservation contributes no line at all.
    fake_db.add_item(
        sku="WIDGET", warehouse_id="w1", quantity=100, price=99.0, tenant_id="tenant-b"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    assert resp.json()["lines"] == []


def test_reserved_value_reservation_without_item_contributes_nothing(client, fake_db):
    fake_db.add_item(
        sku="GADGET", warehouse_id="w1", quantity=100, price=5.0, tenant_id="tenant-a"
    )
    fake_db.add_reservation(
        order_id="o1", tenant_id="tenant-a", sku="GADGET", warehouse_id="w1", quantity=2
    )
    fake_db.add_reservation(
        order_id="o2",
        tenant_id="tenant-a",
        sku="MISSING",
        warehouse_id="w1",
        quantity=7,
    )
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    lines = resp.json()["lines"]
    assert [
        (line["sku"], line["reserved_qty"], line["reserved_value"]) for line in lines
    ] == [("GADGET", 2, 10.0)]


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
    for sku in ("A", "B", "C"):
        fake_db.add_item(sku=sku, warehouse_id="w1", quantity=1, tenant_id="tenant-a")


def _quantities(fake_db):
    return {r["sku"]: r["quantity"] for r in fake_db.items}


def test_import_snapshot_malformed_last_entry_writes_nothing(client, fake_db):
    _seed_import_items(fake_db)
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
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}


def test_import_snapshot_write_failure_rolls_back_all_entries(
    client, fake_db, monkeypatch
):
    import copy
    from contextlib import contextmanager

    from app.routes import reports as reports_routes

    _seed_import_items(fake_db)

    class _FailingCursor:
        """Applies writes to the fake store, raising on the Nth statement."""

        def __init__(self, fail_on: int):
            self.calls = 0
            self.fail_on = fail_on

        def execute(self, sql, params=()):
            self.calls += 1
            if self.calls == self.fail_on:
                raise RuntimeError("simulated write failure")
            fake_db.execute(sql, params)

    class _Conn:
        def cursor(self, cursor_factory=None):
            return _FailingCursor(fail_on=2)

    @contextmanager
    def rollback_transaction():
        # Mirrors app.db.transaction(): commit on success, roll back on error.
        snapshot = copy.deepcopy(fake_db.items)
        try:
            yield _Conn()
        except Exception:
            fake_db.items = snapshot
            raise

    monkeypatch.setattr(reports_routes, "transaction", rollback_transaction)

    no_raise_client = type(client)(client.app, raise_server_exceptions=False)
    resp = no_raise_client.post(
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
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}


def test_import_snapshot_uses_single_transaction(client, fake_db, monkeypatch):
    from contextlib import contextmanager

    from app.routes import reports as reports_routes

    _seed_import_items(fake_db)
    opened = []
    real_transaction = fake_db.transaction

    @contextmanager
    def counting_transaction():
        opened.append(1)
        with real_transaction() as conn:
            yield conn

    monkeypatch.setattr(reports_routes, "transaction", counting_transaction)
    resp = client.post(
        "/reports/import",
        json={
            "items": [
                {"sku": "A", "warehouse_id": "w1", "quantity": 10},
                {"sku": "B", "warehouse_id": "w1", "quantity": 20},
            ]
        },
        headers=TENANT_A,
    )
    assert resp.status_code == 200
    assert len(opened) == 1


def test_import_snapshot_valid_list_applies_every_entry(client, fake_db):
    _seed_import_items(fake_db)
    fake_db.add_item(sku="A", warehouse_id="w1", quantity=7, tenant_id="tenant-b")
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
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == 3
    assert body["snapshot"] == '{"received": 3}'
    tenant_a = {
        r["sku"]: r["quantity"] for r in fake_db.items if r["tenant_id"] == "tenant-a"
    }
    assert tenant_a == {"A": 10, "B": 20, "C": 30}
    # Tenant scoping on the writes is preserved.
    other = next(r for r in fake_db.items if r["tenant_id"] == "tenant-b")
    assert other["quantity"] == 7


def test_import_snapshot_rejects_negative_or_non_dict_entries(client, fake_db):
    _seed_import_items(fake_db)
    for bad in ({"sku": "B", "warehouse_id": "w1", "quantity": -1}, "oops"):
        resp = client.post(
            "/reports/import",
            json={"items": [{"sku": "A", "warehouse_id": "w1", "quantity": 10}, bad]},
            headers=TENANT_A,
        )
        assert resp.status_code == 400
    assert _quantities(fake_db) == {"A": 1, "B": 1, "C": 1}
