from datetime import datetime

from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app
from findr.adapters.inbound.http.routers.search_router import _source_url
from findr.adapters.outbound.elasticsearch.search_index_elasticsearch import ElasticsearchIndex
from findr.adapters.outbound.postgres.document_repository_postgres import (
    DocumentRepositoryPostgres,
)
from findr.adapters.outbound.postgres.source_connection_repo_postgres import (
    SourceConnectionRepositoryPostgres,
)
from findr.domain.entities import Document, SearchHit
from findr.domain.value_objects import SourceType


def _hit(source_type, external_id, external_account=None) -> SearchHit:
    document = Document(
        id=1,
        user_id=1,
        connection_id=1,
        external_id=external_id,
        subject=None,
        sender=None,
        recipients=None,
        body_text=None,
        sent_at=None,
    )
    return SearchHit(
        document=document, snippet="", score=1.0, source_type=source_type,
        external_account=external_account,
    )


def test_source_url_for_gmail():
    assert _source_url(_hit(SourceType.GMAIL, "1a0d80362b124e4d")) == (
        "https://mail.google.com/mail/u/0/#all/1a0d80362b124e4d"
    )


def test_source_url_for_uploaded_files_is_our_copy_of_the_original():
    assert _source_url(_hit(SourceType.FILE, "0b1c2d3e-uuid")) == "/documents/1/file"


DEMO_LOGIN = {"email": "demouser", "password": "password@2050"}


def _login_with_workspace(client, name="Client A") -> int:
    client.post("/auth/login", json=DEMO_LOGIN)
    return client.post("/workspaces", json={"name": name}).json()["id"]


def test_search_endpoint_requires_login(app_env):
    with TestClient(app) as client:
        resp = client.get("/workspaces/1/search", params={"q": "renewal"})
        assert resp.status_code == 401


def test_old_user_wide_search_endpoint_is_gone(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        assert client.get("/search", params={"q": "renewal"}).status_code == 404


def _seed_document(
    user_id: int, workspace_id: int, *, subject: str, body_text: str, sent_at=None,
    account: str = "a@gmail.com",
) -> None:
    # Writes through both stores directly, the way SyncSource would: insert
    # into Postgres (to get a real, DB-assigned id), then index the
    # persisted document into the app's own Elasticsearch index.
    db = app.state.session_factory()
    try:
        connection = SourceConnectionRepositoryPostgres(db).create(
            user_id, workspace_id, SourceType.GMAIL, account
        )
        db.commit()
        persisted = DocumentRepositoryPostgres(db).upsert_many(
            [
                Document(
                    id=0,
                    user_id=user_id,
                    connection_id=connection.id,
                    external_id=f"msg-{workspace_id}",
                    subject=subject,
                    sender="x@y.com",
                    recipients="Inbox",
                    body_text=body_text,
                    sent_at=sent_at,
                )
            ]
        )
        db.commit()
    finally:
        db.close()

    search_index = ElasticsearchIndex(
        app.state.es_client,
        app.state.settings.elasticsearch_index,
        app.state.embedding_provider,
    )
    search_index.index_documents(persisted, SourceType.GMAIL, account, workspace_id)


def test_search_returns_only_the_current_workspaces_documents(app_env):
    with TestClient(app) as client:
        a = _login_with_workspace(client, "Client A")
        b = client.post("/workspaces", json={"name": "Client B"}).json()["id"]
        user_id = client.get("/auth/me").json()["id"]
        _seed_document(user_id, a, subject="Q3 renewal terms", body_text="please review the renewal terms")
        _seed_document(
            user_id, b, subject="B renewal", body_text="client B renewal", account="b@gmail.com"
        )

        resp = client.get(f"/workspaces/{a}/search", params={"q": "renewal"})
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [r["subject"] for r in results] == ["Q3 renewal terms"]
        assert results[0]["source_type"] == "gmail"
        assert results[0]["recipients"] == "Inbox"
        assert results[0]["url"] == f"https://mail.google.com/mail/u/0/#all/msg-{a}"
        # The frontend's renderSnippet() JS turns "[" / "]" into <strong>
        # tags — pins the end-to-end highlight response shape.
        assert "[renewal]" in results[0]["snippet"].lower()

        b_results = client.get(f"/workspaces/{b}/search", params={"q": "renewal"}).json()["results"]
        assert [r["subject"] for r in b_results] == ["B renewal"]


def test_search_in_someone_elses_or_a_missing_workspace_is_404(app_env):
    with TestClient(app) as client:
        a = _login_with_workspace(client)
        assert client.get(f"/workspaces/{a + 999}/search", params={"q": "x"}).status_code == 404


def test_search_endpoint_serializes_documents_with_a_sent_at_timestamp(app_env):
    with TestClient(app) as client:
        a = _login_with_workspace(client)
        user_id = client.get("/auth/me").json()["id"]
        _seed_document(
            user_id,
            a,
            subject="Q3 renewal terms",
            body_text="please review the renewal terms",
            sent_at=datetime(2026, 9, 20, 14, 30, 0),
        )

        resp = client.get(f"/workspaces/{a}/search", params={"q": "renewal"})

        assert resp.status_code == 200
        assert resp.json()["results"][0]["sent_at"] == "2026-09-20T14:30:00"
