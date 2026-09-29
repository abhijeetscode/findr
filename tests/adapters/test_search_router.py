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


def test_source_url_for_notion_strips_dashes():
    page_id = "550e8400-e29b-41d4-a716-446655440000"
    assert _source_url(_hit(SourceType.NOTION, page_id)) == (
        "https://www.notion.so/550e8400e29b41d4a716446655440000"
    )


def test_source_url_for_slack_uses_channel_and_team_id():
    hit = _hit(SourceType.SLACK, "C123:1700000000.000100", external_account="T456:U789")
    assert _source_url(hit) == "https://app.slack.com/client/T456/C123"


def test_source_url_for_slack_returns_none_without_team_id():
    hit = _hit(SourceType.SLACK, "C123:1700000000.000100", external_account=None)
    assert _source_url(hit) is None


def test_search_endpoint_requires_login(app_env):
    with TestClient(app) as client:
        resp = client.get("/search", params={"q": "renewal"})
        assert resp.status_code == 401


def _seed_document(client, user_id: int, *, subject: str, body_text: str, sent_at=None) -> None:
    # Writes through both stores directly, the way SyncSource would: insert
    # into Postgres (to get a real, DB-assigned id), then index the
    # persisted document into the app's own Elasticsearch client/index
    # (app.state.*, set up by the running app's lifespan) — a document
    # inserted only into Postgres is invisible to /search, since there's no
    # trigger keeping Elasticsearch in sync the way SQLite FTS5 had.
    db = app.state.session_factory()
    try:
        connection = SourceConnectionRepositoryPostgres(db).create(
            user_id, SourceType.GMAIL, "a@gmail.com"
        )
        db.commit()
        persisted = DocumentRepositoryPostgres(db).upsert_many(
            [
                Document(
                    id=0,
                    user_id=user_id,
                    connection_id=connection.id,
                    external_id="msg-1",
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

    search_index = ElasticsearchIndex(app.state.es_client, app.state.settings.elasticsearch_index)
    search_index.index_documents(persisted, SourceType.GMAIL, "a@gmail.com")


def test_search_endpoint_returns_only_the_logged_in_users_documents(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "demouser", "password": "password@2050"})
        user_id = client.get("/auth/me").json()["id"]

        _seed_document(
            client, user_id, subject="Q3 renewal terms", body_text="please review the renewal terms"
        )

        resp = client.get("/search", params={"q": "renewal"})
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert results[0]["subject"] == "Q3 renewal terms"
        assert results[0]["source_type"] == "gmail"
        assert results[0]["recipients"] == "Inbox"
        assert results[0]["url"] == "https://mail.google.com/mail/u/0/#all/msg-1"
        # The frontend's renderSnippet() JS turns "[" / "]" into <strong>
        # tags — pins the end-to-end highlight response shape, not just the
        # adapter-level ES config.
        assert "[renewal]" in results[0]["snippet"].lower()


def test_search_endpoint_serializes_documents_with_a_sent_at_timestamp(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "demouser", "password": "password@2050"})
        user_id = client.get("/auth/me").json()["id"]

        _seed_document(
            client,
            user_id,
            subject="Q3 renewal terms",
            body_text="please review the renewal terms",
            sent_at=datetime(2026, 9, 20, 14, 30, 0),
        )

        resp = client.get("/search", params={"q": "renewal"})

        assert resp.status_code == 200
        assert resp.json()["results"][0]["sent_at"] == "2026-09-20T14:30:00"
