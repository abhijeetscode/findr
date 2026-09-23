from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

app = FastAPI()

STATIC_DIR = Path(__file__).parent
INDEX_HTML = STATIC_DIR / "Unified Search Interface.html"


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML)


@app.get("/healthz")
async def healthz():
    return {"error": False}

