def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_version(client):
    resp = client.get("/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": "1.0.0"}
