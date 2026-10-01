import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy.orm import sessionmaker

from findr.adapters.inbound.http.routers.auth_router import router as auth_router
from findr.adapters.inbound.http.routers.documents_router import router as documents_router
from findr.adapters.inbound.http.routers.search_router import router as search_router
from findr.adapters.inbound.http.routers.sources_router import router as sources_router
from findr.adapters.inbound.http.routers.sources_router import (
    workspace_router as workspace_sources_router,
)
from findr.adapters.inbound.http.routers.uploads_router import router as uploads_router
from findr.adapters.inbound.http.routers.uploads_router import (
    workspace_router as workspace_uploads_router,
)
from findr.adapters.inbound.http.routers.workspaces_router import router as workspaces_router
from findr.adapters.outbound.crypto.password_hasher_argon2 import Argon2Hasher
from findr.adapters.outbound.elasticsearch.es_client import create_es_client, ensure_index
from findr.adapters.outbound.embeddings.sentence_transformer_provider import (
    create_embedding_provider,
)
from findr.adapters.outbound.postgres.db import create_db_engine, init_db
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.adapters.outbound.scheduler.sync_scheduler import create_sync_scheduler
from findr.adapters.taskiq.broker import broker
from findr.adapters.taskiq.tasks import process_upload
from findr.adapters.taskiq.upload_queue_taskiq import TaskiqUploadQueue
from findr.application.auth.register_user import RegisterUser
from findr.config import Settings
from findr.domain.exceptions import DuplicateUser
from findr.ports.embedding_provider import EmbeddingProvider
from findr.ports.upload_queue import UploadQueue

# src/findr/adapters/inbound/http/app.py -> parents[3] == src/findr/
FINDR_PACKAGE_DIR = Path(__file__).resolve().parents[3]
INDEX_HTML = FINDR_PACKAGE_DIR / "Unified Search Interface.html"


def _ensure_demo_user(session_factory: sessionmaker, settings: Settings) -> None:
    """There's no public sign-up (see auth_router.py) — this seeds the one
    fixed demo account on every startup, idempotently, so it's always
    available to log in with."""
    db = session_factory()
    try:
        use_case = RegisterUser(UserRepositoryPostgres(db), Argon2Hasher())
        try:
            use_case.execute(settings.demo_username, settings.demo_password)
            db.commit()
        except DuplicateUser:
            db.rollback()
    finally:
        db.close()


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Module-level so tests can monkeypatch it with a fake instead of
    loading the real model on every TestClient startup."""
    return create_embedding_provider(settings)


async def start_upload_queue() -> UploadQueue:
    """Connects to Redis for enqueuing upload jobs. Module-level so tests
    can swap in a fake (see stop_upload_queue)."""
    await broker.startup()
    return TaskiqUploadQueue(process_upload, asyncio.get_running_loop())


async def stop_upload_queue() -> None:
    await broker.shutdown()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    engine = create_db_engine(settings.database_url)
    init_db(engine)
    session_factory = sessionmaker(bind=engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    es_client = create_es_client(settings.elasticsearch_url)
    ensure_index(es_client, settings.elasticsearch_index)
    app.state.es_client = es_client

    # Loaded once and shared by request handlers and the scheduler — the
    # model is ~1GB+ in memory. Fails startup if it can't load, same as
    # Postgres/Elasticsearch (specs/semantic-search.md §6).
    embedding_provider = build_embedding_provider(settings)
    app.state.embedding_provider = embedding_provider

    _ensure_demo_user(session_factory, settings)

    # Uploads are processed by the separate worker service; the API only
    # enqueues (specs/upload-chunking.md §5).
    upload_queue = await start_upload_queue()
    app.state.upload_queue = upload_queue

    # Background sync (APScheduler, in-process). MVP constraint: run with a
    # single worker / no --reload, or multiple schedulers would double-sync
    # — see specs/gmail-connector.md section 8.
    scheduler = create_sync_scheduler(
        session_factory, settings, es_client, embedding_provider, upload_queue
    )
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await stop_upload_queue()
        engine.dispose()
        es_client.close()


app = FastAPI(lifespan=lifespan)
app.include_router(auth_router)
app.include_router(search_router)
app.include_router(sources_router)
app.include_router(documents_router)
app.include_router(uploads_router)
app.include_router(workspaces_router)
app.include_router(workspace_sources_router)
app.include_router(workspace_uploads_router)


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML)


@app.get("/healthz")
async def healthz():
    return {"error": False}
