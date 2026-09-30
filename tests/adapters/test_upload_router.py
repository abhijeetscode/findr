from pathlib import Path

from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app
from findr.adapters.outbound.crypto.password_hasher_argon2 import Argon2Hasher
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.application.auth.register_user import RegisterUser

DEMO_LOGIN = {"email": "demouser", "password": "password@2050"}


def _upload(client, filename, content, content_type="text/plain"):
    return client.post("/sources/upload", files={"file": (filename, content, content_type)})


def _storage_root() -> Path:
    return Path(app.state.settings.upload_storage_root)


def _stored_files() -> list[Path]:
    root = _storage_root()
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_upload_and_delete_require_login(app_env):
    with TestClient(app) as client:
        assert _upload(client, "a.txt", b"hello").status_code == 401
        assert client.delete("/documents/1").status_code == 401


def test_uploaded_file_becomes_searchable_and_listed_as_a_source(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)

        content = b"# Renewal\n\nPlease review the renewal terms."
        # Browsers often send .md as octet-stream; the extension decides.
        resp = _upload(client, "q3-terms.md", content, "application/octet-stream")

        assert resp.status_code == 201
        body = resp.json()
        assert body["filename"] == "q3-terms.md"
        assert body["mime_type"] == "text/markdown"
        assert body["file_size_bytes"] == len(content)
        assert len(_stored_files()) == 1
        assert _stored_files()[0].read_bytes().startswith(b"# Renewal")

        results = client.get("/search", params={"q": "renewal"}).json()["results"]
        assert len(results) == 1
        assert results[0]["document_id"] == body["document_id"]
        assert results[0]["source_type"] == "file"
        assert results[0]["subject"] == "q3-terms.md"
        assert results[0]["url"] is None
        assert "[renewal]" in results[0]["snippet"].lower()

        sources = client.get("/sources").json()
        assert [(s["source_type"], s["display_name"]) for s in sources] == [
            ("file", "Uploaded files")
        ]
        assert sources[0]["id"] == body["connection_id"]


def test_second_upload_reuses_the_uploaded_files_connection(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)

        first = _upload(client, "notes.txt", b"first").json()
        second = _upload(client, "notes.txt", b"second").json()

        assert first["connection_id"] == second["connection_id"]
        assert first["document_id"] != second["document_id"]
        assert len(client.get("/sources").json()) == 1
        assert len(_stored_files()) == 2


def test_unsupported_or_empty_upload_is_rejected_and_writes_nothing(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)

        unsupported = _upload(client, "photo.png", b"\x89PNG\r\n", "image/png")
        empty = _upload(client, "blank.txt", b"   \n")

        assert unsupported.status_code == 422
        assert "Unsupported file type" in unsupported.json()["detail"]
        assert empty.status_code == 422
        assert _stored_files() == []
        assert client.get("/sources").json() == []


def test_delete_document_removes_it_from_search_and_disk(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        document_id = _upload(client, "renewal.txt", b"renewal terms").json()["document_id"]
        assert len(_stored_files()) == 1

        resp = client.delete(f"/documents/{document_id}")

        assert resp.status_code == 204
        assert client.get("/search", params={"q": "renewal"}).json()["results"] == []
        assert _stored_files() == []
        assert client.delete(f"/documents/{document_id}").status_code == 404


def test_delete_another_users_document_is_404_and_leaves_it_intact(app_env):
    with TestClient(app) as client:
        db = app.state.session_factory()
        try:
            RegisterUser(UserRepositoryPostgres(db), Argon2Hasher()).execute(
                "other@example.com", "other-password"
            )
            db.commit()
        finally:
            db.close()

        client.post("/auth/login", json=DEMO_LOGIN)
        document_id = _upload(client, "renewal.txt", b"renewal terms").json()["document_id"]
        client.post("/auth/logout")

        client.post("/auth/login", json={"email": "other@example.com", "password": "other-password"})
        assert client.delete(f"/documents/{document_id}").status_code == 404
        assert client.get("/search", params={"q": "renewal"}).json()["results"] == []
        client.post("/auth/logout")

        client.post("/auth/login", json=DEMO_LOGIN)
        assert len(client.get("/search", params={"q": "renewal"}).json()["results"]) == 1
        assert len(_stored_files()) == 1


def test_uploaded_files_connection_cannot_be_resynced_or_disconnected(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        connection_id = _upload(client, "a.txt", b"hello").json()["connection_id"]

        assert client.post(f"/sources/{connection_id}/sync").status_code == 409
        assert client.delete(f"/sources/{connection_id}").status_code == 409
        assert client.get("/sources").json()[0]["status"] == "active"
