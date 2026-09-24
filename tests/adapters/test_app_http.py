from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app


def test_healthz_returns_ok():
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"error": False}
