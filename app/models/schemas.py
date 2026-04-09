"""
Pydantic models for request/response schemas.
"""

from pydantic import AliasChoices, BaseModel, Field, field_validator
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

    @field_validator("keywords", mode="before")
    @classmethod
    def clean_keywords(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            return v
        out: List[str] = []
        seen: set = set()
        for x in v:
            if x is None:
                continue
            s = str(x).strip()
            if not s:
                continue
            key = s.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out
    use_playwright: bool = Field(
        default=True,
        validation_alias=AliasChoices("use_playwright", "use_selenium"),
        description="Use Playwright (Chromium) for JS-rendered Steam pages. Falls back to httpx if false.",
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
