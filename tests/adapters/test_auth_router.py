from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app
from findr.adapters.inbound.http.deps import SESSION_COOKIE_NAME

DEMO_EMAIL = "demouser"
DEMO_PASSWORD = "password@2050"


def test_register_endpoint_no_longer_exists(monkeypatch):
    # There's no public sign-up — the app seeds a single fixed demo account
    # on startup instead (see app.py's _ensure_demo_user).
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    with TestClient(app) as client:
        assert client.post(
            "/auth/register", json={"email": "a@example.com", "password": "correct-horse-1"}
        ).status_code == 404


def test_login_logout_flow_with_seeded_demo_account(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")

    with TestClient(app) as client:
        bad_login = client.post(
            "/auth/login", json={"email": DEMO_EMAIL, "password": "wrong-password"}
        )
        assert bad_login.status_code == 401
        assert SESSION_COOKIE_NAME not in bad_login.cookies

        login_resp = client.post(
            "/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
        )
        assert login_resp.status_code == 200
        assert login_resp.json()["email"] == DEMO_EMAIL
        assert SESSION_COOKIE_NAME in login_resp.cookies

        logout_resp = client.post("/auth/logout")
        assert logout_resp.status_code == 204


def test_me_requires_login(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    with TestClient(app) as client:
        assert client.get("/auth/me").status_code == 401


def test_me_returns_current_user_after_login(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})

        resp = client.get("/auth/me")

        assert resp.status_code == 200
        assert resp.json()["email"] == DEMO_EMAIL


def test_me_returns_401_after_logout(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
        client.post("/auth/logout")

        assert client.get("/auth/me").status_code == 401
