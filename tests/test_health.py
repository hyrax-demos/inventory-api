from app import cache, config


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_details_empty_cache(client):
    resp = client.get("/health/details")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "version": config.VERSION,
        "cache_entries": 0,
    }


def test_health_details_counts_only_live_entries(client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cache, "_now", lambda: now[0])
    cache.put("stock:a", 1, ttl=5.0)
    cache.put("stock:b", 2, ttl=5.0)
    cache.put("price:w1:a", 3.0, ttl=1.0)

    assert client.get("/health/details").json()["cache_entries"] == 3

    now[0] += 2.0  # the 1s entry has expired, the 5s ones have not
    body = client.get("/health/details").json()
    assert body["cache_entries"] == 2
    # Counting is read-only: the expired entry is not evicted by the probe.
    assert len(cache._store) == 3


def test_health_details_reports_configured_version(client, monkeypatch):
    monkeypatch.setattr(config, "VERSION", "9.9.9")
    assert client.get("/health/details").json()["version"] == "9.9.9"
