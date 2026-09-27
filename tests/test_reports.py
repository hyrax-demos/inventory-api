from datetime import datetime, timedelta, timezone

import pytest

import app.routes.reports as reports_routes

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
        sku="WIDGET", warehouse_id="w1", delta=-2, created_at=datetime.now(timezone.utc), tenant_id="tenant-a"
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert any(m["sku"] == "WIDGET" for m in body["movements"])


def test_todays_movements_scoped_to_tenant(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET", warehouse_id="w1", delta=-2, created_at=datetime.now(timezone.utc), tenant_id="tenant-b"
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["movements"] == []


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


# --- UTC day boundary for /reports/today -----------------------------------

# A fixed instant: 2024-03-10 02:30 UTC. On a server at UTC-05:00 the local
# wall clock reads 2024-03-09 21:30, so a naive-local "start of today" would
# be 2024-03-09 00:00 -- a full day too early relative to UTC.
_FIXED_UTC_NOW = datetime(2024, 3, 10, 2, 30, tzinfo=timezone.utc)
_SERVER_TZ = timezone(timedelta(hours=-5))


class _FrozenNonUTCServerDatetime(datetime):
    """``datetime`` whose clock is frozen and whose *local* zone is UTC-05:00."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _FIXED_UTC_NOW.astimezone(_SERVER_TZ).replace(tzinfo=None)
        return _FIXED_UTC_NOW.astimezone(tz)


@pytest.fixture
def frozen_non_utc_clock(monkeypatch):
    monkeypatch.setattr(reports_routes, "datetime", _FrozenNonUTCServerDatetime)


def test_utc_start_of_day_is_tz_aware_utc_midnight():
    start = reports_routes._utc_start_of_day(_FIXED_UTC_NOW)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.tzinfo is not None
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_converts_aware_input_to_utc_date():
    # 21:30 on the 9th at UTC-05:00 is already the 10th in UTC.
    local = _FIXED_UTC_NOW.astimezone(_SERVER_TZ)
    assert local.date().isoformat() == "2024-03-09"
    start = reports_routes._utc_start_of_day(local)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes._utc_start_of_day(datetime(2024, 3, 10, 2, 30))  # noqa: DTZ001


def test_utc_start_of_day_defaults_to_current_utc_date():
    start = reports_routes._utc_start_of_day()
    now = datetime.now(timezone.utc)
    assert start.utcoffset() == timedelta(0)
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert start <= now < start + timedelta(days=1, seconds=1)


def test_todays_movements_passes_aware_utc_midnight_to_query(
    client, monkeypatch, frozen_non_utc_clock
):
    captured = {}

    def fake_fetch_all(sql, params):
        captured["params"] = params
        return []

    monkeypatch.setattr(reports_routes, "fetch_all", fake_fetch_all)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    cutoff = captured["params"][1]
    assert cutoff.tzinfo is not None
    assert cutoff == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert cutoff.utcoffset() == timedelta(0)
    assert resp.json()["date"] == "2024-03-10"


def test_todays_movements_uses_utc_day_on_non_utc_server(
    client, fake_db, frozen_non_utc_clock
):
    # Stored in UTC: one movement from yesterday (UTC), one from today (UTC).
    # The "yesterday" one falls after local (UTC-05:00) midnight, so a
    # naive-local cutoff would wrongly include it.
    fake_db.add_movement(
        sku="YESTERDAY",
        warehouse_id="w1",
        delta=-1,
        created_at=datetime(2024, 3, 9, 23, 0, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="TODAY",
        warehouse_id="w1",
        delta=-2,
        created_at=datetime(2024, 3, 10, 0, 15, tzinfo=timezone.utc),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-10"
    assert [m["sku"] for m in body["movements"]] == ["TODAY"]
