from pathlib import Path

from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app
from findr.adapters.outbound.crypto.password_hasher_argon2 import Argon2Hasher
from findr.adapters.outbound.files.unstructured_document_parser import (
    UnstructuredDocumentParser,
)
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.adapters.taskiq.tasks import WorkerResources, run_process_upload
from findr.application.auth.register_user import RegisterUser
from findr.application.uploads.process_upload import Outcome

DEMO_LOGIN = {"email": "demouser", "password": "password@2050"}


def _login(client, login=DEMO_LOGIN, workspace="Client A") -> int:
    """Logs in and returns the id of a (new) workspace for this user."""
    client.post("/auth/login", json=login)
    return client.post("/workspaces", json={"name": workspace}).json()["id"]


def _upload(client, workspace_id, filename, content, content_type="text/plain"):
    return client.post(
        f"/workspaces/{workspace_id}/uploads", files={"file": (filename, content, content_type)}
    )


def _drain_queue(upload_queue) -> list[Outcome]:
    """Plays the worker: runs the real worker entry point (run_process_upload)
    for every enqueued upload, with the app's own DB/ES/embedding resources
    and the real Unstructured parser."""
    resources = WorkerResources(
        settings=app.state.settings,
        session_factory=app.state.session_factory,
        es_client=app.state.es_client,
        embedding_provider=app.state.embedding_provider,
        document_parser=UnstructuredDocumentParser(),
    )
    outcomes = [run_process_upload(resources, upload_id) for upload_id in upload_queue.enqueued]
    upload_queue.enqueued.clear()
    return outcomes


def _stored_files() -> list[Path]:
    root = Path(app.state.settings.upload_storage_root)
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def _search(client, workspace_id, q):
    return client.get(f"/workspaces/{workspace_id}/search", params={"q": q}).json()["results"]


def test_upload_endpoints_require_login(app_env):
    with TestClient(app) as client:
        assert _upload(client, 1, "a.txt", b"hello").status_code == 401
        assert client.get("/workspaces/1/uploads").status_code == 401
        assert client.get("/uploads/1").status_code == 401
        assert client.delete("/uploads/1").status_code == 401
        assert client.delete("/documents/1").status_code == 401


def test_old_user_wide_upload_endpoints_are_gone(app_env):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        assert client.post("/sources/upload", files={"file": ("a.txt", b"x")}).status_code in (404, 405)
        assert client.get("/uploads").status_code in (404, 405)


def test_upload_is_accepted_queued_then_processed_into_search(app_env, upload_queue):
    with TestClient(app) as client:
        ws = _login(client)
        content = b"# Renewal\n\nPlease review the renewal terms.\n\n# Support\n\nSupport hours are nine to five."

        # Browsers often send .md as octet-stream; the extension decides.
        resp = _upload(client, ws, "q3-terms.md", content, "application/octet-stream")

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending"
        assert body["mime_type"] == "text/markdown"
        assert body["file_size_bytes"] == len(content)
        assert upload_queue.enqueued == [body["upload_id"]]
        assert len(_stored_files()) == 1
        # Nothing searchable until the worker has run.
        assert _search(client, ws, "renewal") == []
        assert client.get(f"/uploads/{body['upload_id']}").json()["status"] == "pending"

        assert _drain_queue(upload_queue) == [Outcome.READY]

        status = client.get(f"/uploads/{body['upload_id']}").json()
        assert status["status"] == "ready"
        assert status["error"] is None
        results = _search(client, ws, "renewal")
        assert len(results) == 1
        assert results[0]["document_id"] == status["document_id"]
        assert results[0]["source_type"] == "file"
        assert results[0]["subject"] == "q3-terms.md"
        assert "[renewal]" in results[0]["snippet"].lower()
        sources = client.get(f"/workspaces/{ws}/sources").json()
        assert [(s["source_type"], s["display_name"]) for s in sources] == [
            ("file", "Uploaded files")
        ]


def test_uploads_in_one_workspace_never_appear_in_another(app_env, upload_queue):
    # The client-isolation guarantee, end to end (specs/workspaces.md §2).
    with TestClient(app) as client:
        a = _login(client, workspace="Client A")
        b = client.post("/workspaces", json={"name": "Client B"}).json()["id"]
        _upload(client, b, "b-contract.txt", b"Client B renewal terms")
        _drain_queue(upload_queue)

        assert len(_search(client, b, "renewal")) == 1
        assert _search(client, a, "renewal") == []
        assert client.get(f"/workspaces/{a}/uploads").json() == []
        assert client.get(f"/workspaces/{a}/sources").json() == []
        assert [u["filename"] for u in client.get(f"/workspaces/{b}/uploads").json()] == [
            "b-contract.txt"
        ]


def test_file_that_cannot_be_parsed_shows_as_failed(app_env, upload_queue):
    with TestClient(app) as client:
        ws = _login(client)
        upload_id = _upload(
            client,
            ws,
            "broken.docx",
            b"not really a docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ).json()["upload_id"]

        assert _drain_queue(upload_queue) == [Outcome.FAILED]

        [listed] = client.get(f"/workspaces/{ws}/uploads").json()
        assert listed["upload_id"] == upload_id
        assert listed["status"] == "failed"
        assert "Could not read this file" in listed["error"]
        assert listed["document_id"] is None


def test_unsupported_or_empty_upload_is_rejected_immediately(app_env, upload_queue):
    with TestClient(app) as client:
        ws = _login(client)

        unsupported = _upload(client, ws, "photo.png", b"\x89PNG\r\n", "image/png")
        empty = _upload(client, ws, "blank.txt", b"")

        assert unsupported.status_code == 422
        assert "Unsupported file type" in unsupported.json()["detail"]
        assert empty.status_code == 422
        assert _stored_files() == []
        assert upload_queue.enqueued == []
        assert client.get(f"/workspaces/{ws}/uploads").json() == []


def test_upload_into_a_workspace_that_isnt_yours_is_404(app_env, upload_queue):
    with TestClient(app) as client:
        ws = _login(client)
        assert _upload(client, ws + 999, "a.txt", b"hello").status_code == 404
        assert client.get(f"/workspaces/{ws + 999}/uploads").status_code == 404
        assert upload_queue.enqueued == []


def test_delete_upload_in_any_status(app_env, upload_queue):
    with TestClient(app) as client:
        ws = _login(client)
        pending_id = _upload(client, ws, "pending.txt", b"lunch plans").json()["upload_id"]
        upload_queue.enqueued.clear()  # never processed
        ready_id = _upload(client, ws, "ready.txt", b"renewal terms").json()["upload_id"]
        _drain_queue(upload_queue)
        assert len(_search(client, ws, "renewal")) == 1

        assert client.delete(f"/uploads/{pending_id}").status_code == 204
        assert client.delete(f"/uploads/{ready_id}").status_code == 204

        assert client.get(f"/workspaces/{ws}/uploads").json() == []
        assert _search(client, ws, "renewal") == []
        assert _stored_files() == []
        assert client.delete(f"/uploads/{ready_id}").status_code == 404


def test_delete_document_still_works_for_a_ready_upload(app_env, upload_queue):
    with TestClient(app) as client:
        ws = _login(client)
        upload_id = _upload(client, ws, "renewal.txt", b"renewal terms").json()["upload_id"]
        _drain_queue(upload_queue)
        document_id = client.get(f"/uploads/{upload_id}").json()["document_id"]

        assert client.delete(f"/documents/{document_id}").status_code == 204

        assert _search(client, ws, "renewal") == []
        assert client.get(f"/uploads/{upload_id}").status_code == 404
        assert _stored_files() == []
        assert client.delete(f"/documents/{document_id}").status_code == 404


def test_other_users_uploads_are_invisible_and_undeletable(app_env, upload_queue):
    with TestClient(app) as client:
        db = app.state.session_factory()
        try:
            RegisterUser(UserRepositoryPostgres(db), Argon2Hasher()).execute(
                "other@example.com", "other-password"
            )
            db.commit()
        finally:
            db.close()

        ws = _login(client)
        upload_id = _upload(client, ws, "renewal.txt", b"renewal terms").json()["upload_id"]
        _drain_queue(upload_queue)
        document_id = client.get(f"/uploads/{upload_id}").json()["document_id"]
        client.post("/auth/logout")

        other_ws = _login(client, {"email": "other@example.com", "password": "other-password"})
        assert client.get(f"/workspaces/{ws}/uploads").status_code == 404
        assert client.get(f"/workspaces/{ws}/search", params={"q": "renewal"}).status_code == 404
        assert client.get(f"/uploads/{upload_id}").status_code == 404
        assert client.delete(f"/uploads/{upload_id}").status_code == 404
        assert client.delete(f"/documents/{document_id}").status_code == 404
        assert _search(client, other_ws, "renewal") == []
        client.post("/auth/logout")

        client.post("/auth/login", json=DEMO_LOGIN)
        assert len(_search(client, ws, "renewal")) == 1
        assert len(_stored_files()) == 1


def test_uploaded_files_connection_cannot_be_resynced_or_disconnected(app_env, upload_queue):
    with TestClient(app) as client:
        ws = _login(client)
        _upload(client, ws, "a.txt", b"hello")
        _drain_queue(upload_queue)
        connection_id = client.get(f"/workspaces/{ws}/sources").json()[0]["id"]

        assert client.post(f"/sources/{connection_id}/sync").status_code == 409
        assert client.delete(f"/sources/{connection_id}").status_code == 409
        assert client.get(f"/workspaces/{ws}/sources").json()[0]["status"] == "active"
