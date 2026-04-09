"""
Pydantic models for request/response schemas.
"""

from pydantic import BaseModel, Field
from typing import List, Optional


# ── Search ──────────────────────────────────────────────────────────────────

class GameResult(BaseModel):
    app_id: int
    name: str
    url: str
    header_image: Optional[str] = None
    short_description: Optional[str] = None
    genres: Optional[List[str]] = None
    price: Optional[str] = None


class SearchResponse(BaseModel):
    query: str
    results: List[GameResult]
    total: int


# ── Scraper ──────────────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    app_id: int
    game_name: str
    keywords: List[str] = Field(
        default=[
            "cheating", "cheat", "scam", "ban", "banned",
            "moderator", "unfair", "hack", "exploit", "toxic",
            "report", "abuse", "grief", "harassment",
        ]
    )
    max_pages: int = Field(default=5, ge=1, le=50)
    use_selenium: bool = Field(
        default=True,
        description="Use Selenium for JS-rendered pages. Falls back to requests if False.",
    )


class FlaggedComment(BaseModel):
    thread_title: str
    thread_url: str
    author: str
    timestamp: Optional[str]
    comment_text: str
    matched_keywords: List[str]
    sentiment: Optional[str] = None        # positive / negative / neutral
    sentiment_score: Optional[float] = None


class ScrapeResult(BaseModel):
    app_id: int
    game_name: str
    discussion_url: str
    pages_scraped: int
    threads_found: int
    comments_scanned: int
    flagged_comments: List[FlaggedComment]
    keyword_frequency: dict
    errors: List[str] = []
