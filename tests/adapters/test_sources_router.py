from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app


def test_sources_endpoints_require_login(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    with TestClient(app) as client:
        assert client.get("/sources").status_code == 401
        assert client.get("/sources/gmail/connect", follow_redirects=False).status_code == 401
        assert client.delete("/sources/1").status_code == 401


def test_gmail_connect_redirects_to_google_with_pkce_params(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    with TestClient(app) as client:
        client.post(
            "/auth/register", json={"email": "a@example.com", "password": "correct-horse-1"}
        )
        client.post("/auth/login", json={"email": "a@example.com", "password": "correct-horse-1"})

        resp = client.get("/sources/gmail/connect", follow_redirects=False)

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=test-client-id" in location
        assert "code_challenge=" in location
        assert "state=" in location
        assert "access_type=offline" in location
        assert "prompt=consent" in location


def test_list_sources_returns_empty_before_connecting_anything(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    with TestClient(app) as client:
        client.post(
            "/auth/register", json={"email": "a@example.com", "password": "correct-horse-1"}
        )
        client.post("/auth/login", json={"email": "a@example.com", "password": "correct-horse-1"})

        resp = client.get("/sources")

        assert resp.status_code == 200
        assert resp.json() == []


def test_disconnect_unknown_connection_returns_404(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    with TestClient(app) as client:
        client.post(
            "/auth/register", json={"email": "a@example.com", "password": "correct-horse-1"}
        )
        client.post("/auth/login", json={"email": "a@example.com", "password": "correct-horse-1"})

        resp = client.delete("/sources/999")

        assert resp.status_code == 404
