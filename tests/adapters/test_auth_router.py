from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app
from findr.adapters.inbound.http.deps import SESSION_COOKIE_NAME


def test_register_login_logout_flow(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")

    with TestClient(app) as client:
        register_resp = client.post(
            "/auth/register", json={"email": "a@example.com", "password": "correct-horse-1"}
        )
        assert register_resp.status_code == 201
        assert register_resp.json()["email"] == "a@example.com"

        dup_resp = client.post(
            "/auth/register", json={"email": "a@example.com", "password": "another-password"}
        )
        assert dup_resp.status_code == 409

        bad_login = client.post(
            "/auth/login", json={"email": "a@example.com", "password": "wrong-password"}
        )
        assert bad_login.status_code == 401
        assert SESSION_COOKIE_NAME not in bad_login.cookies

        login_resp = client.post(
            "/auth/login", json={"email": "a@example.com", "password": "correct-horse-1"}
        )
        assert login_resp.status_code == 200
        assert SESSION_COOKIE_NAME in login_resp.cookies

        logout_resp = client.post("/auth/logout")
        assert logout_resp.status_code == 204
