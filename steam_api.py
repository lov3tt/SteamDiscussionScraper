"""
Steam Store API — game search service.

Uses the Steam Store search endpoint (no API key required) plus
the Steam App Details API for enriched metadata.
"""

import httpx
import asyncio
from typing import List, Optional
from app.models.schemas import GameResult

STEAM_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
STEAM_APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_STORE_BASE = "https://store.steampowered.com/app"


async def search_games(query: str, max_results: int = 10) -> List[GameResult]:
    """
    Search the Steam Store for games matching `query`.
    Returns a list of GameResult objects enriched with metadata.
    """
    params = {
        "term": query,
        "l": "english",
        "cc": "US",
        "category1": 998,   # 998 = Games only
        "count": max_results,
    }

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(STEAM_SEARCH_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    items = data.get("items", [])
    results: List[GameResult] = []

    for item in items:
        app_id: Optional[int] = item.get("id")
        name: str = item.get("name", "Unknown")
        if not app_id:
            continue

        # Basic price parsing
        price_str = _parse_price(item)

        # Optional: small logo thumbnail from search payload
        header = item.get("logo") or item.get("tiny_image") or None

        results.append(
            GameResult(
                app_id=app_id,
                name=name,
                url=f"{STEAM_STORE_BASE}/{app_id}",
                header_image=header,
                short_description=None,    # enriched separately if needed
                genres=None,
                price=price_str,
            )
        )

    return results


async def get_app_details(app_id: int) -> Optional[GameResult]:
    """
    Fetch full app metadata from Steam App Details API.
    Returns None if the app is not found or not a game.
    """
    params = {"appids": app_id, "cc": "US", "l": "english"}

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(STEAM_APP_DETAILS_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    app_data = data.get(str(app_id), {})
    if not app_data.get("success"):
        return None

    detail = app_data["data"]
    genres = [g["description"] for g in detail.get("genres", [])]
    price_overview = detail.get("price_overview", {})
    price = price_overview.get("final_formatted") or "Free"

    return GameResult(
        app_id=app_id,
        name=detail.get("name", "Unknown"),
        url=f"{STEAM_STORE_BASE}/{app_id}",
        header_image=detail.get("header_image"),
        short_description=detail.get("short_description"),
        genres=genres,
        price=price,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_price(item: dict) -> str:
    price_obj = item.get("price")
    if not price_obj:
        return "Free"
    if isinstance(price_obj, dict):
        final = price_obj.get("final", 0)
        currency = price_obj.get("currency", "USD")
        if final == 0:
            return "Free"
        return f"{currency} {final / 100:.2f}"
    return str(price_obj)
