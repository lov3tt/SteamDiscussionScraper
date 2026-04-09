"""
/api/scrape — Discussion board scraping endpoints
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from app.models.schemas import ScrapeRequest, ScrapeResult
from app.services.scraper import run_scrape_pipeline
import asyncio

router = APIRouter()

# In-memory job store (swap for Redis in production)
_jobs: dict = {}
_job_counter = 0


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
            use_selenium=request.use_selenium,
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/start", summary="Start a background scrape job")
async def scrape_async(request: ScrapeRequest, background_tasks: BackgroundTasks):
    """
    Starts a background scrape job and returns a job_id.
    Poll /api/scrape/status/{job_id} to check progress and retrieve results.
    """
    global _job_counter
    _job_counter += 1
    job_id = str(_job_counter)
    _jobs[job_id] = {"status": "running", "result": None, "error": None}

    async def _run():
        try:
            result = await run_scrape_pipeline(
                app_id=request.app_id,
                game_name=request.game_name,
                keywords=request.keywords,
                max_pages=request.max_pages,
                use_selenium=request.use_selenium,
            )
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["result"] = result.model_dump()
        except Exception as exc:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)

    background_tasks.add_task(_run)
    return {"job_id": job_id, "status": "running"}


@router.get("/status/{job_id}", summary="Poll background scrape job status")
async def scrape_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
