from datetime import datetime

from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app
from findr.adapters.inbound.http.routers.sources_router import _to_response
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.domain.entities import SourceConnection
from findr.domain.value_objects import ConnectionStatus, SourceType


def test_to_response_falls_back_to_external_account_when_no_display_name():
    connection = SourceConnection(
        id=1,
        user_id=1,
        source_type=SourceType.GMAIL,
        external_account="a@gmail.com",
        status=ConnectionStatus.ACTIVE,
        sync_cursor=None,
        last_synced_at=None,
        last_error=None,
        created_at=datetime(2024, 1, 1),
    )

    assert _to_response(connection).display_name == "a@gmail.com"


def test_to_response_prefers_display_name_when_set():
    connection = SourceConnection(
        id=1,
        user_id=1,
        source_type=SourceType.SLACK,
        external_account="T1:U1",
        status=ConnectionStatus.ACTIVE,
        sync_cursor=None,
        last_synced_at=None,
        last_error=None,
        created_at=datetime(2024, 1, 1),
        display_name="Acme Corp (Ada Lovelace)",
    )

    assert _to_response(connection).display_name == "Acme Corp (Ada Lovelace)"


def test_sources_endpoints_require_login(app_env):
    with TestClient(app) as client:
        assert client.get("/sources").status_code == 401
        assert client.get("/sources/gmail/connect", follow_redirects=False).status_code == 401
        assert client.get("/sources/slack/connect", follow_redirects=False).status_code == 401
        assert client.get("/sources/notion/connect", follow_redirects=False).status_code == 401
        assert client.delete("/sources/1").status_code == 401


def test_gmail_connect_redirects_to_google_with_pkce_params(app_env, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "demouser", "password": "password@2050"})

        resp = client.get("/sources/gmail/connect", follow_redirects=False)

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=test-client-id" in location
        assert "code_challenge=" in location
        assert "state=" in location
        assert "access_type=offline" in location
        assert "prompt=consent" in location


def test_slack_connect_redirects_to_slack_with_user_scopes(app_env, monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "test-slack-client-id")
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "demouser", "password": "password@2050"})

        resp = client.get("/sources/slack/connect", follow_redirects=False)

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://slack.com/oauth/v2/authorize?")
        assert "client_id=test-slack-client-id" in location
        assert "user_scope=" in location
        assert "state=" in location


def test_notion_connect_redirects_to_notion(app_env, monkeypatch):
    monkeypatch.setenv("NOTION_CLIENT_ID", "test-notion-client-id")
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "demouser", "password": "password@2050"})

        resp = client.get("/sources/notion/connect", follow_redirects=False)

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert location.startswith("https://api.notion.com/v1/oauth/authorize?")
        assert "client_id=test-notion-client-id" in location
        assert "state=" in location


def test_list_sources_returns_empty_before_connecting_anything(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "demouser", "password": "password@2050"})

        resp = client.get("/sources")

        assert resp.status_code == 200
        assert resp.json() == []


def test_disconnect_unknown_connection_returns_404(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "demouser", "password": "password@2050"})

        resp = client.delete("/sources/999")

        assert resp.status_code == 404


def test_resync_requires_login(app_env):
    with TestClient(app) as client:
        assert client.post("/sources/1/sync").status_code == 401


def test_resync_unknown_connection_returns_404(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "demouser", "password": "password@2050"})

        resp = client.post("/sources/999/sync")

        assert resp.status_code == 404


def test_resync_disconnected_connection_returns_409(app_env, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "demouser", "password": "password@2050"})
        user_id = client.get("/auth/me").json()["id"]

        db = app.state.session_factory()
        try:
            connection_repo = SourceConnectionRepositoryPostgres(db)
            connection = connection_repo.create(user_id, SourceType.GMAIL, "a@gmail.com")
            connection_repo.update_status(connection.id, ConnectionStatus.DISCONNECTED)
            db.commit()
        finally:
            db.close()

        resp = client.post(f"/sources/{connection.id}/sync")

        assert resp.status_code == 409
