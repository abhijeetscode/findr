from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app
from findr.adapters.outbound.sqlite.document_repository_sqlite import DocumentRepositorySqlite
from findr.adapters.outbound.sqlite.source_connection_repo_sqlite import (
    SourceConnectionRepositorySqlite,
)
from findr.domain.entities import Document
from findr.domain.value_objects import SourceType


def test_search_endpoint_requires_login(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    with TestClient(app) as client:
        resp = client.get("/search", params={"q": "renewal"})
        assert resp.status_code == 401


def test_search_endpoint_returns_only_the_logged_in_users_documents(monkeypatch):
    monkeypatch.setenv("FINDR_DATABASE_PATH", ":memory:")
    with TestClient(app) as client:
        register_resp = client.post(
            "/auth/register", json={"email": "a@example.com", "password": "correct-horse-1"}
        )
        user_id = register_resp.json()["id"]
        client.post(
            "/auth/login", json={"email": "a@example.com", "password": "correct-horse-1"}
        )

        # Insert directly through the repository, against the same
        # in-memory engine the app's lifespan created (StaticPool keeps it
        # shared across sessions).
        db = app.state.session_factory()
        try:
            connection = SourceConnectionRepositorySqlite(db).create(
                user_id, SourceType.GMAIL, "a@gmail.com"
            )
            db.commit()
            DocumentRepositorySqlite(db).upsert_many(
                [
                    Document(
                        id=0,
                        user_id=user_id,
                        connection_id=connection.id,
                        external_id="msg-1",
                        subject="Q3 renewal terms",
                        sender="x@y.com",
                        recipients=None,
                        body_text="please review the renewal terms",
                        sent_at=None,
                    )
                ]
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/search", params={"q": "renewal"})
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 1
        assert results[0]["subject"] == "Q3 renewal terms"
