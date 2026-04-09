"""
/api/search — Steam game search endpoints
"""

from fastapi import APIRouter, Query, HTTPException
from app.services.steam_api import search_games, get_app_details
from app.models.schemas import SearchResponse, GameResult

router = APIRouter()


@router.get("/", response_model=SearchResponse, summary="Search Steam games by name")
async def search(
    q: str = Query(..., min_length=2, description="Game name query"),
    max_results: int = Query(default=10, ge=1, le=20),
):
    """
    Uses the Steam Store search API to find games matching the query.
    Returns app IDs, names, prices, and store URLs.
    """
    try:
        results = await search_games(q, max_results)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Steam API error: {exc}")

    return SearchResponse(query=q, results=results, total=len(results))


@router.get("/details/{app_id}", response_model=GameResult, summary="Get game details by App ID")
async def game_details(app_id: int):
    """
    Fetches full metadata for a game from the Steam App Details API.
    """
    try:
        result = await get_app_details(app_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Steam API error: {exc}")

    if not result:
        raise HTTPException(status_code=404, detail=f"App {app_id} not found or not a game.")

    return result
