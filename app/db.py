"""
app/db.py — PostgreSQL database layer
──────────────────────────────────────
Handles:
  - Persistent browser session storage (cookies/localStorage → BYTEA)
  - Thread HTML caching (avoid re-fetching with Playwright)
  - Parsed comment caching (JSONB — avoid re-parsing)
  - Scrape results caching (JSONB)
  - Download manifest with timestamps
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger(__name__)


def _jsonb_to_python(value: Any) -> Any:
    """asyncpg decodes JSONB to dict/list; keep json.loads for str/legacy rows."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        return json.loads(value)
    return value


# ── Connection ───────────────────────────────────────────────────────────────

_pool: Optional[asyncpg.Pool] = None

import os

# 1. Get the URL from the environment
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    # 2. Fix the protocol for asyncpg if it's the old 'postgres://' style
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    # 3. ONLY use localhost if the environment variable doesn't exist at all
    DATABASE_URL = "postgresql://postgres:postgres@localhost:15432/steam_scraper"

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # Use the finalized DATABASE_URL here
        _pool = await asyncpg.create_pool(
            DATABASE_URL, 
            min_size=2, 
            max_size=10,
            ssl="require" 
        )
    return _pool



async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Schema Setup ─────────────────────────────────────────────────────────────

DDL = """
-- Browser session persistence (Playwright storageState JSON)
CREATE TABLE IF NOT EXISTS browser_sessions (
    id          SERIAL PRIMARY KEY,
    label       TEXT NOT NULL UNIQUE DEFAULT 'default',
    state_json  TEXT NOT NULL,              -- JSON from context.storage_state()
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Raw HTML cache per thread URL (avoid Playwright re-fetch)
CREATE TABLE IF NOT EXISTS page_html_cache (
    url         TEXT PRIMARY KEY,
    html        TEXT NOT NULL,              -- raw Playwright page content
    fetched_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '24 hours')
);
CREATE INDEX IF NOT EXISTS idx_page_html_expires ON page_html_cache(expires_at);

-- Parsed comments cache per thread URL (avoid re-parsing BeautifulSoup)
CREATE TABLE IF NOT EXISTS thread_comments_cache (
    url           TEXT PRIMARY KEY,
    thread_title  TEXT NOT NULL DEFAULT '',
    comments      JSONB NOT NULL,           -- List[{author, timestamp, text}]
    parsed_at     TIMESTAMPTZ DEFAULT NOW(),
    expires_at    TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '7 days')
);
CREATE INDEX IF NOT EXISTS idx_thread_comments_expires ON thread_comments_cache(expires_at);

-- Thread list cache per app_id + page (avoid re-fetching listing pages)
CREATE TABLE IF NOT EXISTS thread_list_cache (
    app_id      INTEGER NOT NULL,
    page        INTEGER NOT NULL,
    threads     JSONB NOT NULL,             -- List[{title, url}]
    fetched_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '6 hours'),
    PRIMARY KEY (app_id, page)
);

-- Full scrape results (the ScrapeResult model as JSONB)
CREATE TABLE IF NOT EXISTS scrape_results (
    id              SERIAL PRIMARY KEY,
    app_id          INTEGER NOT NULL,
    game_name       TEXT NOT NULL,
    keywords        JSONB NOT NULL,         -- sorted list for cache-key matching
    max_pages       INTEGER NOT NULL,
    result          JSONB NOT NULL,         -- full ScrapeResult dict
    scraped_at      TIMESTAMPTZ DEFAULT NOW(),
    -- fast lookups
    UNIQUE (app_id, keywords, max_pages)
);
CREATE INDEX IF NOT EXISTS idx_scrape_results_app ON scrape_results(app_id);
CREATE INDEX IF NOT EXISTS idx_scrape_results_scraped ON scrape_results(scraped_at DESC);
"""


async def init_db() -> None:
    """Run DDL — idempotent, safe to call on every startup."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(DDL)
    logger.info("Database schema initialised.")


# ── Browser Session ───────────────────────────────────────────────────────────

async def save_browser_session(state_json: str, label: str = "default") -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO browser_sessions (label, state_json, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (label) DO UPDATE
              SET state_json = EXCLUDED.state_json,
                  updated_at = NOW()
            """,
            label,
            state_json,
        )


async def load_browser_session(label: str = "default") -> Optional[str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state_json FROM browser_sessions WHERE label = $1", label
        )
    return row["state_json"] if row else None


# ── Page HTML Cache ────────────────────────────────────────────────────────────

async def get_cached_html(url: str) -> Optional[str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT html FROM page_html_cache WHERE url = $1 AND expires_at > NOW()",
            url,
        )
    return row["html"] if row else None


async def set_cached_html(url: str, html: str, ttl_hours: int = 24) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO page_html_cache (url, html, fetched_at, expires_at)
            VALUES ($1, $2, NOW(), NOW() + ($3 || ' hours')::INTERVAL)
            ON CONFLICT (url) DO UPDATE
              SET html = EXCLUDED.html,
                  fetched_at = NOW(),
                  expires_at = EXCLUDED.expires_at
            """,
            url,
            html,
            str(ttl_hours),
        )


async def invalidate_html_cache(app_id: Optional[int] = None) -> int:
    """Expire HTML cache. If app_id given, only for that game's threads."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if app_id:
            result = await conn.execute(
                "DELETE FROM page_html_cache WHERE url LIKE $1",
                f"%/app/{app_id}/%",
            )
        else:
            result = await conn.execute("DELETE FROM page_html_cache")
    return int(result.split()[-1])


# ── Thread Comments Cache ─────────────────────────────────────────────────────

async def get_cached_comments(url: str) -> Optional[Dict]:
    """Returns {thread_title, comments: List[dict], parsed_at} or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT thread_title, comments, parsed_at
            FROM thread_comments_cache
            WHERE url = $1 AND expires_at > NOW()
            """,
            url,
        )
    if not row:
        return None
    return {
        "thread_title": row["thread_title"],
        "comments": _jsonb_to_python(row["comments"]),
        "parsed_at": row["parsed_at"],
    }


async def set_cached_comments(
    url: str,
    thread_title: str,
    comments: List[Dict],
    ttl_days: int = 7,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO thread_comments_cache
                (url, thread_title, comments, parsed_at, expires_at)
            VALUES ($1, $2, $3::jsonb, NOW(), NOW() + ($4 || ' days')::INTERVAL)
            ON CONFLICT (url) DO UPDATE
              SET thread_title = EXCLUDED.thread_title,
                  comments     = EXCLUDED.comments,
                  parsed_at    = NOW(),
                  expires_at   = EXCLUDED.expires_at
            """,
            url,
            thread_title,
            json.dumps(comments),
            str(ttl_days),
        )


async def invalidate_comments_cache(app_id: Optional[int] = None) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if app_id:
            result = await conn.execute(
                "DELETE FROM thread_comments_cache WHERE url LIKE $1",
                f"%/app/{app_id}/%",
            )
        else:
            result = await conn.execute("DELETE FROM thread_comments_cache")
    return int(result.split()[-1])


# ── Thread List Cache ─────────────────────────────────────────────────────────

async def get_cached_thread_list(app_id: int, page: int) -> Optional[List[Dict]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT threads FROM thread_list_cache
            WHERE app_id = $1 AND page = $2 AND expires_at > NOW()
            """,
            app_id,
            page,
        )
    return _jsonb_to_python(row["threads"]) if row else None


async def set_cached_thread_list(
    app_id: int, page: int, threads: List[Dict], ttl_hours: int = 6
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO thread_list_cache (app_id, page, threads, fetched_at, expires_at)
            VALUES ($1, $2, $3::jsonb, NOW(), NOW() + ($4 || ' hours')::INTERVAL)
            ON CONFLICT (app_id, page) DO UPDATE
              SET threads    = EXCLUDED.threads,
                  fetched_at = NOW(),
                  expires_at = EXCLUDED.expires_at
            """,
            app_id,
            page,
            json.dumps(threads),
            str(ttl_hours),
        )


async def invalidate_thread_list_cache(app_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM thread_list_cache WHERE app_id = $1", app_id
        )
    return int(result.split()[-1])


# ── Scrape Results ────────────────────────────────────────────────────────────

def _kw_key(keywords: List[str]) -> str:
    """Canonical JSON key for a keyword list (sorted, lower-cased)."""
    return json.dumps(sorted(k.casefold() for k in keywords))


async def get_cached_scrape_result(
    app_id: int, keywords: List[str], max_pages: int
) -> Optional[Dict]:
    """Return {result, scraped_at} if a cached scrape matches exactly."""
    pool = await get_pool()
    kw_json = _kw_key(keywords)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT result, scraped_at
            FROM scrape_results
            WHERE app_id = $1
              AND keywords = $2::jsonb
              AND max_pages = $3
            ORDER BY scraped_at DESC
            LIMIT 1
            """,
            app_id,
            kw_json,
            max_pages,
        )
    if not row:
        return None
    return {
        "result": _jsonb_to_python(row["result"]),
        "scraped_at": row["scraped_at"],
    }


async def save_scrape_result(
    app_id: int,
    game_name: str,
    keywords: List[str],
    max_pages: int,
    result: Dict,
) -> datetime:
    """Upsert a scrape result; returns the scraped_at timestamp."""
    pool = await get_pool()
    kw_json = _kw_key(keywords)
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scrape_results
                (app_id, game_name, keywords, max_pages, result, scraped_at)
            VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6)
            ON CONFLICT (app_id, keywords, max_pages) DO UPDATE
              SET game_name  = EXCLUDED.game_name,
                  result     = EXCLUDED.result,
                  scraped_at = EXCLUDED.scraped_at
            """,
            app_id,
            game_name,
            kw_json,
            max_pages,
            json.dumps(result),
            now,
        )
    return now


async def list_scrape_results(app_id: Optional[int] = None) -> List[Dict]:
    """List cached scrape results (summary only — no full result payload)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if app_id:
            rows = await conn.fetch(
                """
                SELECT id, app_id, game_name, keywords, max_pages, scraped_at
                FROM scrape_results
                WHERE app_id = $1
                ORDER BY scraped_at DESC
                """,
                app_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, app_id, game_name, keywords, max_pages, scraped_at
                FROM scrape_results
                ORDER BY scraped_at DESC
                """
            )
    return [
        {
            "id": r["id"],
            "app_id": r["app_id"],
            "game_name": r["game_name"],
            "keywords": _jsonb_to_python(r["keywords"]),
            "max_pages": r["max_pages"],
            "scraped_at": r["scraped_at"].isoformat(),
        }
        for r in rows
    ]


async def get_scrape_result_by_id(result_id: int) -> Optional[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT result, scraped_at, game_name FROM scrape_results WHERE id = $1",
            result_id,
        )
    if not row:
        return None
    return {
        "result": _jsonb_to_python(row["result"]),
        "scraped_at": row["scraped_at"],
        "game_name": row["game_name"],
    }


async def delete_scrape_result(result_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM scrape_results WHERE id = $1", result_id
        )
    return result.split()[-1] == "1"


# ── Cache Stats ───────────────────────────────────────────────────────────────

async def get_cache_stats() -> Dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        html_total = await conn.fetchval("SELECT COUNT(*) FROM page_html_cache")
        html_live = await conn.fetchval(
            "SELECT COUNT(*) FROM page_html_cache WHERE expires_at > NOW()"
        )
        comments_total = await conn.fetchval("SELECT COUNT(*) FROM thread_comments_cache")
        comments_live = await conn.fetchval(
            "SELECT COUNT(*) FROM thread_comments_cache WHERE expires_at > NOW()"
        )
        thread_lists = await conn.fetchval(
            "SELECT COUNT(*) FROM thread_list_cache WHERE expires_at > NOW()"
        )
        scrape_count = await conn.fetchval("SELECT COUNT(*) FROM scrape_results")
    return {
        "html_cache": {"total": html_total, "live": html_live},
        "comments_cache": {"total": comments_total, "live": comments_live},
        "thread_list_cache": {"live": thread_lists},
        "scrape_results": scrape_count,
    }
