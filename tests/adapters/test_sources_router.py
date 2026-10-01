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
        source_type=SourceType.GMAIL,
        external_account="ada@acme.com",
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
