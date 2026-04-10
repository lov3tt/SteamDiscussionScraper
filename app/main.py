"""
app/main.py — FastAPI app with PostgreSQL lifespan
"""

from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import app.db as db
from app.routers import scrape, search

# Project root (parent of app/) — index.html lives here; optional static/ for assets
_ROOT = Path(__file__).resolve().parent.parent
_STATIC_DIR = _ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialise DB pool + run DDL
    await db.init_db()
    yield
    # Shutdown: close pool
    await db.close_pool()


app = FastAPI(
    title="Steam NLP Scraper",
    version="2.0.0",
    description="Steam discussion board scraper with PostgreSQL caching",
    lifespan=lifespan,
)

app.include_router(search.router, prefix="/api/search", tags=["Search"])
app.include_router(scrape.router, prefix="/api/scrape", tags=["Scrape"])

_STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/api/health")
async def health():
    stats = await db.get_cache_stats()
    return {"status": "ok", "cache": stats}


@app.get("/")
async def index():
    return FileResponse(_ROOT / "index.html")
