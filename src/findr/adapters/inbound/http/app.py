from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from sqlalchemy.orm import sessionmaker

from findr.adapters.inbound.http.routers.auth_router import router as auth_router
from findr.adapters.outbound.sqlite.db import create_db_engine, init_db
from findr.config import Settings

# src/findr/adapters/inbound/http/app.py -> parents[3] == src/findr/
FINDR_PACKAGE_DIR = Path(__file__).resolve().parents[3]
INDEX_HTML = FINDR_PACKAGE_DIR / "Unified Search Interface.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    engine = create_db_engine(settings.database_path)
    init_db(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessionmaker(bind=engine)
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(auth_router)


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML)


@app.get("/healthz")
async def healthz():
    return {"error": False}
