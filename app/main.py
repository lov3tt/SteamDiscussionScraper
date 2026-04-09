"""
Steam Discussion Board NLP Scraper — FastAPI Backend
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from app.routers import search, scrape

app = FastAPI(
    title="Steam Discussion NLP Scraper",
    description="Search Steam games, scrape discussion boards, and detect flagged keywords via NLP.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(search.router, prefix="/api/search", tags=["Game Search"])
app.include_router(scrape.router, prefix="/api/scrape", tags=["Discussion Scraper"])

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_index_html = os.path.join(_project_root, "index.html")


@app.get("/", include_in_schema=False)
async def serve_frontend():
    if not os.path.isfile(_index_html):
        return PlainTextResponse(
            "Frontend not found. Expected index.html next to the app package.",
            status_code=404,
        )
    return FileResponse(_index_html)


@app.get("/api/health")
async def health():
    return {"status": "ok", "message": "Steam NLP Scraper is running"}
