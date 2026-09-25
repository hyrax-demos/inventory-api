import os
import time
from datetime import datetime, timedelta, timezone

import pytest

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


# -- /reports/today: UTC day boundary --------------------------------------


def _capture_movements_params(monkeypatch, fake_db):
    captured = {}
    real_fetch_all = fake_db.fetch_all

    def spy(sql, params=()):
        if "FROM movements" in sql:
            captured["params"] = params
        return real_fetch_all(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)
    return captured


def test_utc_start_of_day_truncates_to_utc_midnight():
    now = datetime(2024, 3, 10, 17, 45, 12, 999, tzinfo=timezone.utc)
    start = reports_routes.utc_start_of_day(now)
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.utcoffset() == timedelta(0)


def test_utc_start_of_day_uses_utc_calendar_date_for_non_utc_input():
    # 20:00 on Mar 9 in UTC-8 is 04:00 on Mar 10 UTC -> UTC day is Mar 10.
    pst = timezone(timedelta(hours=-8))
    start = reports_routes.utc_start_of_day(datetime(2024, 3, 9, 20, 0, tzinfo=pst))
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert start.tzinfo == timezone.utc

    # 02:00 on Mar 11 in UTC+9 is 17:00 on Mar 10 UTC -> UTC day is Mar 10.
    jst = timezone(timedelta(hours=9))
    start = reports_routes.utc_start_of_day(datetime(2024, 3, 11, 2, 0, tzinfo=jst))
    assert start == datetime(2024, 3, 10, tzinfo=timezone.utc)


def test_utc_start_of_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        reports_routes.utc_start_of_day(datetime(2024, 3, 10, 12, 0))  # noqa: DTZ001


def test_todays_movements_query_boundary_is_aware_utc_midnight(
    client, fake_db, monkeypatch
):
    fixed_now = datetime(2024, 3, 10, 3, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utc_now", lambda: fixed_now)
    captured = _capture_movements_params(monkeypatch, fake_db)

    resp = client.get("/reports/today", headers=TENANT_A)

    assert resp.status_code == 200
    assert resp.json()["date"] == "2024-03-10"
    tenant_id, cutoff = captured["params"]
    assert tenant_id == "tenant-a"
    assert cutoff.tzinfo is not None
    assert cutoff.utcoffset() == timedelta(0)
    assert cutoff == datetime(2024, 3, 10, tzinfo=timezone.utc)


def test_todays_movements_filters_on_utc_day_boundary(client, fake_db, monkeypatch):
    fixed_now = datetime(2024, 3, 10, 3, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(reports_routes, "_utc_now", lambda: fixed_now)
    utc = timezone.utc
    for sku, created_at in [
        ("YESTERDAY", datetime(2024, 3, 9, 23, 59, 59, tzinfo=utc)),
        ("MIDNIGHT", datetime(2024, 3, 10, 0, 0, 0, tzinfo=utc)),
        ("EARLY", datetime(2024, 3, 10, 1, 15, tzinfo=utc)),
    ]:
        fake_db.add_movement(
            sku=sku,
            warehouse_id="w1",
            delta=1,
            created_at=created_at,
            tenant_id="tenant-a",
        )

    resp = client.get("/reports/today", headers=TENANT_A)

    assert resp.status_code == 200
    assert [m["sku"] for m in resp.json()["movements"]] == ["MIDNIGHT", "EARLY"]


@pytest.fixture
def server_local_tz(request):
    """Temporarily switch the process-local timezone (POSIX only)."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires time.tzset (POSIX)")
@pytest.mark.parametrize(
    "server_local_tz", ["America/Los_Angeles", "Asia/Tokyo"], indirect=True
)
def test_todays_movements_independent_of_server_local_timezone(
    client, fake_db, monkeypatch, server_local_tz
):
    captured = _capture_movements_params(monkeypatch, fake_db)
    before = datetime.now(timezone.utc)
    resp = client.get("/reports/today", headers=TENANT_A)
    after = datetime.now(timezone.utc)

    assert resp.status_code == 200
    _, cutoff = captured["params"]
    assert cutoff.utcoffset() == timedelta(0)
    expected = {
        before.replace(hour=0, minute=0, second=0, microsecond=0),
        after.replace(hour=0, minute=0, second=0, microsecond=0),
    }
    assert cutoff in expected
    assert resp.json()["date"] == cutoff.date().isoformat()
