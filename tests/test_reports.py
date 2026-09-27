import time
from datetime import datetime, timedelta, timezone

import pytest

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


def test_todays_movements_returns_recent_entries(client, fake_db):
    fake_db.add_movement(
        sku="WIDGET",
        warehouse_id="w1",
        delta=-2,
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
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


def _pin_utcnow(monkeypatch, instant):
    from app.routes import reports as reports_routes

    monkeypatch.setattr(reports_routes, "_utcnow", lambda: instant)


def test_utc_start_of_day_returns_aware_utc_midnight():
    from app.routes.reports import utc_start_of_day

    result = utc_start_of_day(
        datetime(2024, 3, 10, 17, 45, 12, 999, tzinfo=timezone.utc)
    )
    assert result == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)


def test_utc_start_of_day_uses_utc_date_not_input_offset_date():
    from app.routes.reports import utc_start_of_day

    # 22:30 on the 10th at UTC-05:00 is 03:30 on the 11th in UTC.
    behind = datetime(2024, 3, 10, 22, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert utc_start_of_day(behind) == datetime(2024, 3, 11, tzinfo=timezone.utc)
    # 02:00 on the 11th at UTC+09:00 is 17:00 on the 10th in UTC.
    ahead = datetime(2024, 3, 11, 2, 0, tzinfo=timezone(timedelta(hours=9)))
    assert utc_start_of_day(ahead) == datetime(2024, 3, 10, tzinfo=timezone.utc)


def test_utc_start_of_day_rejects_naive_datetime():
    from app.routes.reports import utc_start_of_day

    with pytest.raises(ValueError):
        utc_start_of_day(datetime(2024, 3, 10, 12, 0))  # noqa: DTZ001


def test_todays_movements_queries_with_aware_utc_midnight(client, fake_db, monkeypatch):
    from app.routes import reports as reports_routes

    _pin_utcnow(monkeypatch, datetime(2024, 3, 10, 17, 45, tzinfo=timezone.utc))
    captured = {}

    def spy(sql, params=()):
        captured["params"] = params
        return fake_db.fetch_all(sql, params)

    monkeypatch.setattr(reports_routes, "fetch_all", spy)
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    tenant_id, cutoff = captured["params"]
    assert tenant_id == "tenant-a"
    assert cutoff == datetime(2024, 3, 10, tzinfo=timezone.utc)
    assert cutoff.tzinfo is not None and cutoff.utcoffset() == timedelta(0)
    assert resp.json()["date"] == "2024-03-10"


def test_todays_movements_filters_on_utc_day_boundary(client, fake_db, monkeypatch):
    _pin_utcnow(monkeypatch, datetime(2024, 3, 10, 1, 30, tzinfo=timezone.utc))
    utc = timezone.utc
    fake_db.add_movement(
        sku="YESTERDAY",
        warehouse_id="w1",
        delta=1,
        created_at=datetime(2024, 3, 9, 23, 59, 59, tzinfo=utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="MIDNIGHT",
        warehouse_id="w1",
        delta=1,
        created_at=datetime(2024, 3, 10, 0, 0, 0, tzinfo=utc),
        tenant_id="tenant-a",
    )
    fake_db.add_movement(
        sku="EARLY",
        warehouse_id="w1",
        delta=1,
        created_at=datetime(2024, 3, 10, 1, 0, tzinfo=utc),
        tenant_id="tenant-a",
    )
    resp = client.get("/reports/today", headers=TENANT_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["date"] == "2024-03-10"
    assert [m["sku"] for m in body["movements"]] == ["MIDNIGHT", "EARLY"]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset unavailable")
@pytest.mark.parametrize("server_tz", ["America/Los_Angeles", "Asia/Tokyo"])
def test_todays_movements_independent_of_server_local_timezone(
    client, fake_db, monkeypatch, server_tz
):
    # Run the real clock under a non-UTC server timezone: the cutoff must
    # still be midnight of the current UTC date.
    monkeypatch.setenv("TZ", server_tz)
    time.tzset()
    try:
        from app.routes import reports as reports_routes

        captured = {}

        def spy(sql, params=()):
            captured["params"] = params
            return fake_db.fetch_all(sql, params)

        monkeypatch.setattr(reports_routes, "fetch_all", spy)
        before = datetime.now(timezone.utc)
        resp = client.get("/reports/today", headers=TENANT_A)
        after = datetime.now(timezone.utc)
        assert resp.status_code == 200
        _, cutoff = captured["params"]
        assert cutoff.tzinfo is not None and cutoff.utcoffset() == timedelta(0)
        expected = {
            datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            for d in (before, after)
        }
        assert cutoff in expected
        assert resp.json()["date"] == cutoff.date().isoformat()
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
