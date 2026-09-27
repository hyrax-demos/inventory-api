from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.routes import reports as reports_routes

TENANT_A = {"X-Tenant-Id": "tenant-a"}


def test_low_stock_report_happy_path(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=2, tenant_id="tenant-a")
    fake_db.add_item(sku="B", name="b", warehouse_id="w1", quantity=99, tenant_id="tenant-a")
    resp = client.get("/reports/low-stock", params={"threshold": 10}, headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_low_stock_report_scoped_to_tenant(client, fake_db):
    fake_db.add_item(sku="A", name="a", warehouse_id="w1", quantity=1, tenant_id="tenant-a")
    fake_db.add_item(sku="B", name="b", warehouse_id="w1", quantity=1, tenant_id="tenant-b")
    resp = client.get("/reports/low-stock", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert [i["sku"] for i in body["items"]] == ["A"]


def test_todays_movements_returns_recent_entries(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET", warehouse_id="w1", delta=-2, created_at=datetime.now(), tenant_id="tenant-a"
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert any(m["sku"] == "WIDGET" for m in body["movements"])


def test_todays_movements_scoped_to_tenant(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET", warehouse_id="w1", delta=-2, created_at=datetime.now(), tenant_id="tenant-b"
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["movements"] == []


def test_todays_movements_uses_utc_midnight_regardless_of_local_time():
    """The query boundary must be UTC midnight, not local-wall-clock midnight.

    Simulate a server whose local clock reads late evening UTC-behind time
    (e.g. 23:30 local, which is already the next UTC day) and assert the
    cutoff passed to the query is timezone-aware and pinned to the current
    *UTC* calendar date at 00:00:00, not derived from naive local time.
    """
    fixed_now_utc = datetime(2024, 3, 15, 2, 0, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                # naive local wall-clock time is "behind" UTC by several
                # hours here, on the *previous* calendar day.
                return datetime(2024, 3, 14, 21, 0, 0)
            return fixed_now_utc.astimezone(tz)

    with patch.object(reports_routes, "datetime", _FixedDatetime):
        captured = {}

        def fake_fetch_all(sql, params):
            captured["params"] = params
            return []

        with patch.object(reports_routes, "fetch_all", fake_fetch_all):
            result = reports_routes.todays_movements(x_tenant_id="tenant-a")

    _, start_of_day = captured["params"]
    assert start_of_day.tzinfo is not None
    assert start_of_day.astimezone(timezone.utc) == datetime(
        2024, 3, 15, 0, 0, 0, tzinfo=timezone.utc
    )
    assert result["date"] == "2024-03-15"


def test_todays_movements_excludes_entry_from_previous_utc_day(client, fake_db):
    """A movement created just before today's UTC midnight must be excluded,
    even though it may still be "today" in the server's local timezone."""
    now_utc = datetime.now(timezone.utc)
    start_of_utc_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    just_before_utc_midnight = start_of_utc_day - timedelta(seconds=1)

    fake_db.add_movement(
        sku="OLD",
        warehouse_id="w1",
        delta=-1,
        created_at=just_before_utc_midnight,
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="NEW",
        warehouse_id="w1",
        delta=-1,
        created_at=now_utc,
        tenant_id="tenant-a",
    )

    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    skus = [m["sku"] for m in body["movements"]]
    assert "NEW" in skus
    assert "OLD" not in skus


def test_reserved_value_happy_path(client, fake_db):
    fake_db.add_item(sku="WIDGET", warehouse_id="w1", quantity=100, price=2.0, tenant_id="tenant-a")
    fake_db.add_reservation(order_id="o1", tenant_id="tenant-a", sku="WIDGET", warehouse_id="w1", quantity=3)
    resp = client.get("/reports/reserved-value", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert line["sku"] == "WIDGET"
    assert line["reserved_qty"] == 3


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
