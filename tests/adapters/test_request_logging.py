"""specs/logging-telemetry.md §4.4, §6, §8, §12: what the running app writes
to its log file."""

import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from findr.adapters.inbound.http.app import app
from findr.adapters.inbound.http.middleware import RequestLoggingMiddleware
from findr.adapters.inbound.http.routers import sources_router
from findr.adapters.outbound.files.unstructured_document_parser import (
    UnstructuredDocumentParser,
)
from findr.adapters.taskiq.tasks import WorkerResources, run_process_upload
from findr.config import Settings
from findr.domain.entities import Credentials
from findr.observability.setup import configure_logging

DEMO_LOGIN = {"email": "demouser", "password": "password@2050"}


@pytest.fixture
def log_dir(tmp_path, monkeypatch) -> Path:
    """The app's lifespan configures logging from the environment. DEBUG
    explicitly, whatever a developer's .env says, so the privacy checks
    below see everything that could be logged."""
    path = tmp_path / "logs"
    monkeypatch.setenv("FINDR_LOG_DIR", str(path))
    monkeypatch.setenv("FINDR_LOG_LEVEL", "DEBUG")
    return path


def _worker_resources():
    return WorkerResources(
        settings=app.state.settings,
        session_factory=app.state.session_factory,
        es_client=app.state.es_client,
        embedding_provider=app.state.embedding_provider,
        document_parser=UnstructuredDocumentParser(),
    )


def read_log(path: Path) -> list[dict]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text().splitlines()]


def _events(path: Path, name: str) -> list[dict]:
    return [r for r in read_log(path) if r.get("event") == name]


def test_startup_is_logged_with_timings(app_env, log_dir):
    with TestClient(app):
        pass
    [started] = _events(log_dir / "api.log", "app.started")
    assert isinstance(started["embedding_model_load_ms"], int)
    assert isinstance(started["db_init_ms"], int)
    assert _events(log_dir / "api.log", "app.stopped")


def test_each_request_gets_an_id_and_one_access_line(app_env, log_dir):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        workspace_id = client.post("/workspaces", json={"name": "Client A"}).json()["id"]
        resp = client.get(f"/workspaces/{workspace_id}/search", params={"q": "renewal"})
        assert resp.status_code == 200

    request_id = resp.headers["X-Request-ID"]
    assert len(request_id) == 16
    lines = [r for r in read_log(log_dir / "api.log") if r.get("request_id") == request_id]
    [access] = [r for r in lines if r["event"] == "http.request"]
    # The route template: no ids, no query string.
    assert access["route"] == "/workspaces/{workspace_id}/search"
    assert (access["method"], access["status"], access["level"]) == ("GET", 200, "INFO")
    assert isinstance(access["duration_ms"], int)
    # Set by the request's dependencies, carried on every line of it.
    assert access["user_id"] == 1
    assert access["workspace_id"] == workspace_id
    [search] = [r for r in lines if r["event"] == "search.executed"]
    assert (search["hits"], search["query_chars"]) == (0, len("renewal"))
    assert search["workspace_id"] == workspace_id
    assert "renewal" not in json.dumps(lines)


def test_a_valid_incoming_request_id_is_kept_and_an_odd_one_replaced(app_env, log_dir):
    with TestClient(app) as client:
        kept = client.get("/auth/me", headers={"X-Request-ID": "from-the-proxy-1"})
        replaced = client.get("/auth/me", headers={"X-Request-ID": "x" * 65})
    assert kept.headers["X-Request-ID"] == "from-the-proxy-1"
    assert replaced.headers["X-Request-ID"] != "x" * 65
    [access] = [
        r
        for r in _events(log_dir / "api.log", "http.request")
        if r["request_id"] == "from-the-proxy-1"
    ]
    # 401 is an expected outcome, not a warning.
    assert (access["status"], access["level"]) == (401, "INFO")


def test_failed_login_is_logged_without_the_username(app_env, log_dir):
    with TestClient(app) as client:
        client.post("/auth/login", json={"email": "someone@client.example", "password": "nope"})
        client.post("/auth/login", json=DEMO_LOGIN)

    [failed] = _events(log_dir / "api.log", "auth.login.failed")
    assert failed["reason"] == "bad_credentials"
    [succeeded] = _events(log_dir / "api.log", "auth.login.succeeded")
    assert succeeded["user_id"] == 1
    text = (log_dir / "api.log").read_text()
    assert "someone@client.example" not in text
    assert "nope" not in text


def test_no_client_names_or_content_in_logs_even_at_debug(app_env, log_dir, upload_queue):
    # specs/logging-telemetry.md Q1/Q2: workspace names, filenames, search
    # text and file content never reach the logs.
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        workspace_id = client.post("/workspaces", json={"name": "Globex Holdings"}).json()["id"]
        client.patch(f"/workspaces/{workspace_id}", json={"name": "Initech Partners"})
        resp = client.post(
            f"/workspaces/{workspace_id}/uploads",
            files={"file": ("AcmeMergerTerms.txt", b"Zanzibar widget pricing", "text/plain")},
        )
        upload_id = resp.json()["upload_id"]
        resources = _worker_resources()
        for queued in upload_queue.enqueued:
            run_process_upload(resources, queued)
        client.get(f"/workspaces/{workspace_id}/search", params={"q": "zanzibar pricing"})
        client.delete(f"/uploads/{upload_id}")
        client.delete(f"/workspaces/{workspace_id}")

    log = log_dir / "api.log"
    text = log.read_text().lower()
    for secret in ("globex", "initech", "acmemerger", "zanzibar", "widget"):
        assert secret not in text
    names = {r.get("event") for r in read_log(log)}
    assert {
        "workspace.created",
        "workspace.renamed",
        "upload.received",
        "upload.processing.started",
        "upload.processed",
        "search.executed",
        "upload.deleted",
        "workspace.deleted",
    } <= names
    [processed] = _events(log, "upload.processed")
    assert processed["outcome"] == "ready"
    assert processed["upload_id"] == upload_id
    assert processed["workspace_id"] == workspace_id
    assert processed["chunks"] >= 1


def test_failed_upload_is_logged_as_a_warning_with_its_reason(app_env, log_dir, upload_queue):
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        workspace_id = client.post("/workspaces", json={"name": "Client A"}).json()["id"]
        client.post(
            f"/workspaces/{workspace_id}/uploads",
            files={
                "file": (
                    "HooliBoardMinutes.docx",
                    b"not really a docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        resources = _worker_resources()
        for queued in upload_queue.enqueued:
            run_process_upload(resources, queued)

    [processed] = _events(log_dir / "api.log", "upload.processed")
    assert (processed["outcome"], processed["level"]) == ("failed", "WARNING")
    assert processed["error_kind"] == "ExtractionFailed"
    assert "hooli" not in (log_dir / "api.log").read_text().lower()


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


def _connect(client, workspace_id):
    start = client.get(f"/workspaces/{workspace_id}/sources/gmail/connect", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(
        "/sources/gmail/callback", params={"code": "c", "state": state}, follow_redirects=False
    )


def test_gmail_connect_is_logged_without_the_address_or_workspace_name(
    app_env, log_dir, monkeypatch
):
    monkeypatch.setattr(
        sources_router, "oauth_provider_for", lambda *_: FakeGoogle("boss@globex.example")
    )
    with TestClient(app) as client:
        client.post("/auth/login", json=DEMO_LOGIN)
        first = client.post("/workspaces", json={"name": "Umbrella Corp"}).json()["id"]
        second = client.post("/workspaces", json={"name": "Wayne Enterprises"}).json()["id"]
        assert _connect(client, first).headers["location"] == "/"
        # Refused: the account is in the first workspace already, and the
        # error message names it.
        refused = _connect(client, second)
        assert "Umbrella Corp" in parse_qs(urlparse(refused.headers["location"]).query)[
            "connect_error"
        ][0]

    log = log_dir / "api.log"
    [connected] = _events(log, "source.connected")
    assert (connected["workspace_id"], connected["reconnect"]) == (first, False)
    [failed] = _events(log, "source.connect.failed")
    assert failed["reason"] == "SourceInOtherWorkspace"
    text = log.read_text().lower()
    for secret in ("globex", "umbrella", "wayne"):
        assert secret not in text


def _broken_app() -> FastAPI:
    broken = FastAPI()
    broken.add_middleware(RequestLoggingMiddleware)

    @broken.get("/items/{item_id}")
    def boom(item_id: int):
        raise RuntimeError("kaboom")

    return broken


def test_unhandled_errors_are_logged_with_a_traceback(tmp_path):
    path = configure_logging(Settings(FINDR_LOG_DIR=str(tmp_path)), "api")
    with TestClient(_broken_app(), raise_server_exceptions=False) as client:
        assert client.get("/items/42?secret=1").status_code == 500

    [failed] = _events(path, "http.request.failed")
    assert failed["level"] == "ERROR"
    assert failed["route"] == "/items/{item_id}"
    assert failed["exc_type"] == "RuntimeError"
    assert "kaboom" in failed["stack"]
    assert len(failed["request_id"]) == 16
    assert "secret" not in path.read_text()


def test_unknown_routes_are_not_logged_by_raw_path(tmp_path):
    path = configure_logging(Settings(FINDR_LOG_DIR=str(tmp_path)), "api")
    with TestClient(_broken_app()) as client:
        assert client.get("/no/such/client-name").status_code == 404
    [access] = _events(path, "http.request")
    assert access["route"] == "<unmatched>"
    assert "client-name" not in path.read_text()
