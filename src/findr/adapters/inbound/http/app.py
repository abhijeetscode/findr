from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy.orm import sessionmaker

from findr.adapters.inbound.http.routers.auth_router import router as auth_router
from findr.adapters.inbound.http.routers.search_router import router as search_router
from findr.adapters.inbound.http.routers.sources_router import router as sources_router
from findr.adapters.outbound.crypto.password_hasher_argon2 import Argon2Hasher
from findr.adapters.outbound.elasticsearch.es_client import create_es_client, ensure_index
from findr.adapters.outbound.postgres.db import create_db_engine, init_db
from findr.adapters.outbound.postgres.user_repository_postgres import UserRepositoryPostgres
from findr.adapters.outbound.scheduler.sync_scheduler import create_sync_scheduler
from findr.application.auth.register_user import RegisterUser
from findr.config import Settings
from findr.domain.exceptions import DuplicateUser

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

    _ensure_demo_user(session_factory, settings)

    # Background sync (APScheduler, in-process). MVP constraint: run with a
    # single worker / no --reload, or multiple schedulers would double-sync
    # — see specs/gmail-connector.md section 8.
    scheduler = create_sync_scheduler(session_factory, settings, es_client)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        engine.dispose()
        es_client.close()


app = FastAPI(lifespan=lifespan)
app.include_router(auth_router)
app.include_router(search_router)
app.include_router(sources_router)


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML)


@app.get("/healthz")
async def healthz():
    return {"error": False}
