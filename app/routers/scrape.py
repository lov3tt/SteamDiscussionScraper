"""
/api/scrape — Discussion board scraping endpoints (PostgreSQL-cached)
"""

import asyncio
import json
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from fastapi.responses import Response

from app.models.schemas import ScrapeRequest, ScrapeResult
from app.services.scraper import run_scrape_pipeline
import app.db as db

router = APIRouter()

# In-memory job store (swap for Redis in production)
_jobs: dict = {}
_job_counter = 0


def _initial_progress(max_pages: int) -> dict:
    return {
        "phase": "starting",
        "max_pages": max_pages,
        "page_index": 0,
        "pages_scraped": 0,
        "threads_discovered": 0,
        "threads_total": 0,
        "thread_index": 0,
        "current_thread": "",
        "comments_scanned": 0,
        "flagged_count": 0,
        "message": "",
    }


# ── Synchronous scrape ────────────────────────────────────────────────────────

@router.post("/", response_model=ScrapeResult, summary="Scrape synchronously (with caching)")
async def scrape_sync(
    request: ScrapeRequest,
    force_refresh: bool = Query(default=False, description="Bypass all caches and re-scrape"),
):
    """
    Scrapes the Steam discussion board for the given app_id.
    Results are persisted in PostgreSQL. Subsequent calls with the same
    app_id + keywords use cached parsed comments — no Playwright needed.
    """
    if not force_refresh:
        cached = await db.get_cached_scrape_result(
            request.app_id, request.keywords, request.max_pages
        )
        if cached:
            return cached["result"]

    try:
        result = await run_scrape_pipeline(
            app_id=request.app_id,
            game_name=request.game_name,
            keywords=request.keywords,
            max_pages=request.max_pages,
            job_state=None,
            force_refresh=force_refresh,
        )
        await db.save_scrape_result(
            app_id=request.app_id,
            game_name=request.game_name,
            keywords=request.keywords,
            max_pages=request.max_pages,
            result=result.model_dump(),
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Async / background scrape ─────────────────────────────────────────────────

@router.post("/start", summary="Start a background scrape job")
async def scrape_async(
    request: ScrapeRequest,
    background_tasks: BackgroundTasks,
    force_refresh: bool = Query(default=False, description="Bypass all caches"),
):
    """
    Starts a background scrape job; returns a job_id.
    Poll GET /api/scrape/status/{job_id} for progress.
    POST /api/scrape/stop/{job_id} to cancel.
    """
    global _job_counter
    _job_counter += 1
    job_id = str(_job_counter)
    cancel_evt = asyncio.Event()
    job_ref = {
        "status": "running",
        "result": None,
        "error": None,
        "progress": _initial_progress(request.max_pages),
        "cancel": cancel_evt,
        "scraped_at": None,
    }
    _jobs[job_id] = job_ref

    async def _run():
        try:
            if not force_refresh:
                cached = await db.get_cached_scrape_result(
                    request.app_id, request.keywords, request.max_pages
                )
                if cached:
                    sa = cached["scraped_at"]
                    job_ref["status"] = "done"
                    job_ref["result"] = cached["result"]
                    job_ref["scraped_at"] = sa.isoformat() if sa else None
                    job_ref["progress"]["message"] = "[CACHE] Using saved scrape result."
                    return

            result = await run_scrape_pipeline(
                app_id=request.app_id,
                game_name=request.game_name,
                keywords=request.keywords,
                max_pages=request.max_pages,
                job_state=job_ref,
                force_refresh=force_refresh,
            )
            scraped_at = await db.save_scrape_result(
                app_id=request.app_id,
                game_name=request.game_name,
                keywords=request.keywords,
                max_pages=request.max_pages,
                result=result.model_dump(),
            )
            job_ref["status"] = "stopped" if cancel_evt.is_set() else "done"
            job_ref["result"] = result.model_dump()
            job_ref["scraped_at"] = scraped_at.isoformat() if scraped_at else None
        except Exception as exc:
            job_ref["status"] = "error"
            job_ref["error"] = str(exc)

    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "running"}


@router.post("/stop/{job_id}", summary="Request cancellation of a running job")
async def scrape_stop(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] == "running":
        ev = job.get("cancel")
        if ev is not None:
            ev.set()
        job["progress"]["message"] = "Stop requested…"
    return {"ok": True, "job_id": job_id, "status": job["status"]}


@router.get("/status/{job_id}", summary="Poll background scrape job status")
async def scrape_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job["status"],
        "result": job["result"],
        "error": job["error"],
        "progress": job.get("progress", {}),
        "scraped_at": job.get("scraped_at"),
    }


# ── Cached results CRUD ───────────────────────────────────────────────────────

@router.get("/results", summary="List all cached scrape results")
async def list_results(app_id: Optional[int] = Query(default=None)):
    """Returns metadata for all saved scrapes (no full payload)."""
    return await db.list_scrape_results(app_id)


@router.get("/results/{result_id}", summary="Get a specific cached scrape result")
async def get_result(result_id: int):
    row = await db.get_scrape_result_by_id(result_id)
    if not row:
        raise HTTPException(status_code=404, detail="Result not found")
    return {
        "result": row["result"],
        "scraped_at": row["scraped_at"].isoformat() if row["scraped_at"] else None,
        "game_name": row["game_name"],
    }


@router.delete("/results/{result_id}", summary="Delete a cached scrape result")
async def delete_result(result_id: int):
    deleted = await db.delete_scrape_result(result_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Result not found")
    return {"ok": True}


# ── Download endpoints ────────────────────────────────────────────────────────

def _download_json_filename(game_name: str, scraped_at) -> str:
    ts = scraped_at.strftime("%Y%m%d_%H%M%S") if scraped_at else "unknown"
    slug = re.sub(r"[^\w.\-]+", "_", (game_name or "scrape").strip())[:40].strip("_") or "scrape"
    return f"steam_scrape_{slug}_{ts}.json"


@router.get("/results/{result_id}/download/json", summary="Download result as JSON")
async def download_json(result_id: int):
    row = await db.get_scrape_result_by_id(result_id)
    if not row:
        raise HTTPException(status_code=404, detail="Result not found")

    scraped_at = row["scraped_at"]
    filename = _download_json_filename(row["game_name"], scraped_at)

    payload = {
        "scraped_at": scraped_at.isoformat() if scraped_at else None,
        "game_name": row["game_name"],
        **row["result"],
    }
    # Compact JSON (no indent) — less RAM and smaller downloads
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # octet-stream + attachment so browsers save a file instead of opening inline JSON
    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Cache management ──────────────────────────────────────────────────────────

@router.get("/cache/stats", summary="Get cache statistics")
async def cache_stats():
    return await db.get_cache_stats()


@router.post("/cache/invalidate", summary="Invalidate page/comment caches for a game")
async def invalidate_cache(
    app_id: int = Query(..., description="App ID to invalidate cache for"),
    level: str = Query(
        default="all",
        description="'html' | 'comments' | 'threads' | 'all'",
    ),
):
    """
    Invalidates caches so the next scrape re-fetches/re-parses.
    Does NOT delete stored ScrapeResults — use DELETE /results/{id} for that.
    """
    counts: dict = {}
    if level in ("html", "all"):
        counts["html_deleted"] = await db.invalidate_html_cache(app_id)
    if level in ("comments", "all"):
        counts["comments_deleted"] = await db.invalidate_comments_cache(app_id)
    if level in ("threads", "all"):
        counts["thread_lists_deleted"] = await db.invalidate_thread_list_cache(app_id)
    return {"ok": True, "app_id": app_id, **counts}
