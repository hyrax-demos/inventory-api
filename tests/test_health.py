def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_version(client):
    resp = client.get("/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": "1.0.0"}


def test_version_content_type_is_json(client):
    resp = client.get("/version")
    assert resp.headers["content-type"].startswith("application/json")


def test_ping(client):
    resp = client.get("/ping")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pong"] is True
    assert isinstance(body["ts"], int)
    assert resp.headers["X-Pong"] == "1"
