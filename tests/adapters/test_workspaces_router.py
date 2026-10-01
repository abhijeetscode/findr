from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from findr.adapters.inbound.http.app import app
from findr.adapters.inbound.http.routers import sources_router
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.oauth_state_repository_postgres import (
    OAuthStateRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.adapters.outbound.postgres.workspace_repository_postgres import (
    WorkspaceRepositoryPostgres,
)
from findr.domain.entities import Credentials, Document
from findr.domain.value_objects import SourceType

DEMO_LOGIN = {"email": "demouser", "password": "password@2050"}


def _create(client, name):
    return client.post("/workspaces", json={"name": name})


# ---------- CRUD ----------


def test_workspace_endpoints_require_login(app_env):
    with TestClient(app) as client:
        assert client.get("/workspaces").status_code == 401
        assert _create(client, "Client A").status_code == 401
        assert client.patch("/workspaces/1", json={"name": "x"}).status_code == 401
        assert client.delete("/workspaces/1").status_code == 401


def test_create_list_and_rename(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        assert client.get("/workspaces").json() == []

        b = _create(client, "  Client B ")
        a = _create(client, "Client A")

        assert b.status_code == 201 and b.json()["name"] == "Client B"
        assert [w["name"] for w in client.get("/workspaces").json()] == ["Client A", "Client B"]

        renamed = client.patch(f"/workspaces/{a.json()['id']}", json={"name": "Client A (Acme)"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Client A (Acme)"


def test_name_errors(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        a = _create(client, "Client A").json()["id"]
        _create(client, "Client B")

        assert _create(client, "client a").status_code == 409
        assert _create(client, "   ").status_code == 422
        assert _create(client, "x" * 101).status_code == 422
        assert client.patch(f"/workspaces/{a}", json={"name": "CLIENT B"}).status_code == 409
        assert client.patch(f"/workspaces/{a + 999}", json={"name": "Nope"}).status_code == 404
        assert client.delete(f"/workspaces/{a + 999}").status_code == 404


# ---------- delete ----------


def _seed_gmail_connection_with_document(user_id: int, workspace_id: int, account: str) -> int:
    db = app.state.session_factory()
    try:
        connection = SourceConnectionRepositoryPostgres(db).create(
            user_id, workspace_id, SourceType.GMAIL, account
        )
        [document] = DocumentRepositoryPostgres(db).upsert_many(
            [
                Document(
                    id=0, user_id=user_id, connection_id=connection.id, external_id=f"m-{account}",
                    subject="Renewal", sender="x@y.com", recipients=None,
                    body_text="renewal terms", sent_at=datetime(2026, 1, 1),
                )
            ]
        )
        db.commit()
    finally:
        db.close()
    ElasticsearchIndex(
        app.state.es_client, app.state.settings.elasticsearch_index, app.state.embedding_provider
    ).index_documents([document], SourceType.GMAIL, account, workspace_id)
    return connection.id


def test_delete_workspace_removes_everything_in_it_and_leaves_others_alone(app_env, upload_queue):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        user_id = client.get("/auth/me").json()["id"]
        a = _create(client, "Client A").json()["id"]
        b = _create(client, "Client B").json()["id"]
        _seed_gmail_connection_with_document(user_id, a, "a@gmail.com")
        _seed_gmail_connection_with_document(user_id, b, "b@gmail.com")
        upload_id = client.post(
            f"/workspaces/{a}/uploads", files={"file": ("a.txt", b"renewal for A", "text/plain")}
        ).json()["upload_id"]

        assert client.delete(f"/workspaces/{a}").status_code == 204

        assert [w["id"] for w in client.get("/workspaces").json()] == [b]
        assert client.get(f"/workspaces/{a}/sources").status_code == 404
        assert client.get(f"/uploads/{upload_id}").status_code == 404
        b_hits = client.get(f"/workspaces/{b}/search", params={"q": "renewal"}).json()["results"]
        assert len(b_hits) == 1
        assert len(client.get(f"/workspaces/{b}/sources").json()) == 1
        # Nothing of A's left in the index, under any filter.
        es = app.state.es_client
        count = es.count(
            index=app.state.settings.elasticsearch_index, query={"term": {"workspace_id": a}}
        )["count"]
        assert count == 0
        # The Gmail address is free to be connected elsewhere now.
        db = app.state.session_factory()
        try:
            assert (
                SourceConnectionRepositoryPostgres(db).get_by_account(
                    user_id, SourceType.GMAIL, "a@gmail.com"
                )
                is None
            )
        finally:
            db.close()


# ---------- Gmail connect across workspaces ----------


class FakeGoogle:
    """Stands in for GmailOAuthProvider so the callback runs without Google."""

    def __init__(self, email):
        self.email = email

    def build_authorize_url(self, state, code_challenge):
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def exchange_code(self, code, code_verifier):
        return Credentials("access", "refresh", datetime(2030, 1, 1))

    def get_account_email(self, access_token):
        return self.email

    def get_display_name(self, access_token):
        return self.email

    def revoke(self, credentials):
        pass


def _connect(client, workspace_id) -> "Response":  # noqa: F821
    start = client.get(f"/workspaces/{workspace_id}/sources/gmail/connect", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(
        "/sources/gmail/callback", params={"code": "c", "state": state}, follow_redirects=False
    )


def test_gmail_account_can_only_be_in_one_workspace(app_env, monkeypatch):
    monkeypatch.setattr(sources_router, "oauth_provider_for", lambda *_: FakeGoogle("me@gmail.com"))
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        a = _create(client, "Client A").json()["id"]
        b = _create(client, "Client B").json()["id"]

        first = _connect(client, a)
        assert first.status_code == 302 and first.headers["location"] == "/"
        assert [s["external_account"] for s in client.get(f"/workspaces/{a}/sources").json()] == [
            "me@gmail.com"
        ]

        refused = _connect(client, b)

        assert refused.status_code == 302
        error = parse_qs(urlparse(refused.headers["location"]).query)["connect_error"][0]
        assert "already connected in workspace 'Client A'" in error
        assert client.get(f"/workspaces/{b}/sources").json() == []

        # Reconnecting into the same workspace still works.
        assert _connect(client, a).headers["location"] == "/"
        assert len(client.get(f"/workspaces/{a}/sources").json()) == 1


def test_connect_started_in_a_since_deleted_workspace_is_rejected(app_env, monkeypatch):
    monkeypatch.setattr(sources_router, "oauth_provider_for", lambda *_: FakeGoogle("me@gmail.com"))
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        a = _create(client, "Client A").json()["id"]
        start = client.get(f"/workspaces/{a}/sources/gmail/connect", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

        client.delete(f"/workspaces/{a}")
        resp = client.get(
            "/sources/gmail/callback", params={"code": "c", "state": state}, follow_redirects=False
        )

        assert resp.status_code == 400


# ---------- Postgres-level guarantees ----------


def test_workspace_names_are_unique_per_user_ignoring_case_in_the_database(db_session):
    users = UserRepositoryPostgres(db_session)
    a = users.create("a@example.com", "hash")
    b = users.create("b@example.com", "hash")
    repo = WorkspaceRepositoryPostgres(db_session)
    repo.create(a.id, "Client A")
    repo.create(a.id, "Client B")
    repo.create(b.id, "Client A")  # another user may reuse the name

    assert repo.get_by_name(a.id, "CLIENT a").name == "Client A"
    with pytest.raises(IntegrityError):
        repo.create(a.id, "client a")


def test_oauth_state_round_trips_its_workspace(db_session):
    user = UserRepositoryPostgres(db_session).create("a@example.com", "hash")
    workspace = WorkspaceRepositoryPostgres(db_session).create(user.id, "Client A")
    states = OAuthStateRepositoryPostgres(db_session)

    state = states.create(user.id, workspace.id, "verifier", 600)

    assert states.consume(state).workspace_id == workspace.id
