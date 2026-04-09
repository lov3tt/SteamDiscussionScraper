"""
/api/scrape — Discussion board scraping endpoints
"""

import asyncio

from fastapi import APIRouter, HTTPException, BackgroundTasks

from app.models.schemas import ScrapeRequest, ScrapeResult
from app.services.scraper import run_scrape_pipeline

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


@router.post("/", response_model=ScrapeResult, summary="Scrape discussion board synchronously")
async def scrape_sync(request: ScrapeRequest):
    """
    Scrapes the Steam discussion board for the given app_id synchronously.
    Returns flagged comments matching the provided keywords with NLP sentiment analysis.

    ⚠️  For large max_pages values, prefer the async /start endpoint.
    """
    try:
        result = await run_scrape_pipeline(
            app_id=request.app_id,
            game_name=request.game_name,
            keywords=request.keywords,
            max_pages=request.max_pages,
            use_playwright=request.use_playwright,
            job_state=None,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/start", summary="Start a background scrape job")
async def scrape_async(request: ScrapeRequest, background_tasks: BackgroundTasks):
    """
    Starts a background scrape job and returns a job_id.
    Poll GET /api/scrape/status/{job_id} for progress; POST /api/scrape/stop/{job_id} to cancel.
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
    }
    _jobs[job_id] = job_ref

    async def _run():
        try:
            result = await run_scrape_pipeline(
                app_id=request.app_id,
                game_name=request.game_name,
                keywords=request.keywords,
                max_pages=request.max_pages,
                use_playwright=request.use_playwright,
                job_state=job_ref,
            )
            job_ref["status"] = "stopped" if cancel_evt.is_set() else "done"
            job_ref["result"] = result.model_dump()
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
    }
