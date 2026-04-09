"""
Steam Discussion Board NLP Scraper — FastAPI Backend
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import os

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

# Serve the frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_frontend():
        return FileResponse(os.path.join(frontend_path, "index.html"))


@app.get("/api/health")
async def health():
    return {"status": "ok", "message": "Steam NLP Scraper is running"}
