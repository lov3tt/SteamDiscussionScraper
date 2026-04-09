"""
Steam Discussion Board Scraper
──────────────────────────────
ETL Pipeline:
  Extract  → Playwright (Chromium) loads JS-rendered discussion pages
  Transform → BeautifulSoup4 parses HTML into thread/comment data
  Load      → NLP keyword matching + VADER sentiment on each comment
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections import Counter
from typing import Any, Callable, Awaitable, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, Tag
import httpx

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    from nltk.sentiment.vader import SentimentIntensityAnalyzer
    import nltk

    try:
        nltk.data.find("sentiment/vader_lexicon.zip")
    except LookupError:
        nltk.download("vader_lexicon", quiet=True)
    _sia = SentimentIntensityAnalyzer()
    VADER_AVAILABLE = True
except Exception:
    VADER_AVAILABLE = False

from app.models.schemas import FlaggedComment, ScrapeResult

logger = logging.getLogger(__name__)

_PLAYWRIGHT_HINT = (
    "Install browsers for the same Python you use to run the app: "
    "python -m playwright install chromium"
)


def _format_playwright_error(exc: Exception) -> str:
    """Playwright sometimes raises errors where str(exc) is empty."""
    name = type(exc).__name__
    text = str(exc).strip()
    if not text:
        text = repr(exc) or name
    alt = getattr(exc, "message", None)
    if alt and str(alt).strip() and str(alt).strip() != text:
        text = f"{text} — {alt}"
    return f"{name}: {text}"


DISCUSSION_BASE = "https://steamcommunity.com/app/{app_id}/discussions/0/"
THREAD_LIST_URL = "https://steamcommunity.com/app/{app_id}/discussions/0/?fp={page}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

FetchFn = Callable[[str, Optional[str], float, bool], Awaitable[str]]
# last bool: is_thread_page (different wait selectors for Steam)

_STEAM_HOST = "https://steamcommunity.com"


def steam_community_url(href: str) -> str:
    """Steam lists often use relative hrefs; Playwright/httpx need absolute URLs."""
    href = (href or "").strip()
    if not href:
        return href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return _STEAM_HOST + href
    return f"{_STEAM_HOST}/{href.lstrip('/')}"


def _class_str(el: Tag) -> str:
    c = el.get("class")
    if not c:
        return ""
    return " ".join(c) if isinstance(c, list) else str(c)


def _parse_thread_links(html: str, app_id: int) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    threads: List[Tuple[str, str]] = []

    for a in soup.select("a.forum_topic_name"):
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if href and str(app_id) in href:
            threads.append((title, steam_community_url(href)))

    if not threads:
        pattern = re.compile(rf"steamcommunity\.com/app/{app_id}/discussions/\d+/\d+")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if pattern.search(href):
                title = a.get_text(strip=True) or href
                threads.append((title, steam_community_url(href)))

    return threads


def _extract_author_from_row(row: Tag) -> str:
    for sel in (
        ".forum_comment_author a",
        ".commentthread_comment_author a",
        ".commentthread_author a",
        ".bbs_author a",
        "a.hub_links",
        ".forum_op_author a",
    ):
        el = row.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t:
                return t
    bdi = row.find("bdi")
    if bdi:
        t = bdi.get_text(strip=True)
        if t:
            return t
    return "Unknown"


def _extract_timestamp_from_row(row: Tag) -> Optional[str]:
    for sel in (
        ".forum_comment_date",
        ".commentthread_comment_timestamp",
        ".commentthread_posttime",
        ".date",
    ):
        el = row.select_one(sel)
        if el:
            t = el.get_text(strip=True) or el.get("title", "")
            if t:
                return t.strip()
    return None


def _extract_body_from_row(row: Tag) -> str:
    selectors = (
        ".forum_comment_text",
        ".commentthread_comment_text",
        ".forum_op_text",
        "div.commentthread_comment_text",
        "[class*='commentthread_comment_text']",
        "[class*='forum_comment_text']",
        ".forum_topic_op_text",
        ".commentthread_comment .content",
        "div.bb_code",
        ".forum_op .content",
    )
    for sel in selectors:
        el = row.select_one(sel)
        if el:
            t = el.get_text(separator=" ", strip=True)
            if len(t) >= 2:
                return t
    # Steam sometimes nests body only under generic wrappers
    for div in row.find_all("div", recursive=True):
        cl = _class_str(div).lower()
        if not cl:
            continue
        if any(
            frag in cl
            for frag in (
                "commenttext",
                "comment_text",
                "postmessage",
                "topicop",
                "forum_op",
                "op_content",
            )
        ):
            t = div.get_text(separator=" ", strip=True)
            if len(t) >= 3:
                return t
    raw = row.get_text(separator=" ", strip=True)
    if len(raw) >= 25:
        return raw
    return ""


def _parse_comments(html: str, thread_title: str, thread_url: str) -> List[Dict]:
    """
    Steam layouts vary: comment body is usually a sibling/descendant of the row,
    not inside a.forum_comment_permlink (which is often just the '#N' anchor).
    """
    soup = BeautifulSoup(html, "html.parser")
    comments: List[Dict] = []
    seen_text: set = set()

    rows: List[Tag] = []
    seen_ids: set = set()

    def add_row(el: Optional[Tag]) -> None:
        if el is None or id(el) in seen_ids:
            return
        seen_ids.add(id(el))
        rows.append(el)

    for div in soup.select("div.commentthread_comment_container"):
        add_row(div)
    for div in soup.select("div.forum_comment"):
        add_row(div)
    for div in soup.select("div.commentthread_comment"):
        add_row(div)

    # Opening post / OP (counts as a comment for keyword scan)
    for div in soup.select("div.forum_op"):
        add_row(div)

    if not rows:
        for a in soup.select("a.forum_comment_permlink"):
            p = a.parent
            for _ in range(12):
                if p is None:
                    break
                if isinstance(p, Tag) and p.name == "div":
                    cl = _class_str(p)
                    if any(
                        x in cl
                        for x in (
                            "forum_comment",
                            "commentthread_comment",
                            "commentthread_area",
                            "ForumTopic",
                        )
                    ):
                        add_row(p)
                        break
                p = p.parent

    for block in rows:
        author = _extract_author_from_row(block)
        timestamp = _extract_timestamp_from_row(block)
        text = _extract_body_from_row(block)
        if not text:
            continue
        key = (author, text[:120])
        if key in seen_text:
            continue
        seen_text.add(key)
        comments.append(
            {
                "thread_title": thread_title,
                "thread_url": thread_url,
                "author": author,
                "timestamp": timestamp,
                "text": text,
            }
        )

    return comments


def _normalize_for_keywords(text: str) -> str:
    """Lowercase, Unicode-normalize, strip invisible chars (Steam / user copy-paste)."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.casefold()
    t = re.sub(r"[\u200b-\u200f\ufeff\u2060\u00ad]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _find_keywords(text: str, keywords: List[str]) -> List[str]:
    """
    Match user keywords against comment text.

    Uses Unicode-aware \\w boundaries (not ASCII-only [a-z0-9]), phrase detection,
    and prefix matching for longer roots (e.g. cheat → cheating) so Steam-style
    prose still hits.
    """
    norm = _normalize_for_keywords(text)
    if not norm:
        return []

    found: List[str] = []
    seen_lower: set = set()

    for kw in keywords:
        if kw is None:
            continue
        original = str(kw).strip()
        if not original:
            continue
        kl = original.casefold()
        if kl in seen_lower:
            continue

        try:
            escaped = re.escape(kl)
        except re.error:
            continue

        # Multi-word phrase: must appear as a contiguous substring
        if " " in kl:
            if kl in norm:
                seen_lower.add(kl)
                found.append(original)
            continue

        # Single "word": Unicode word boundaries (\\w matches letters including Cyrillic, etc.)
        if re.search(rf"(?<!\w){escaped}(?!\w)", norm, flags=re.UNICODE):
            seen_lower.add(kl)
            found.append(original)
            continue

        # Longer roots (>= 4): prefix inside a larger token — cheat/cheating/cheated
        if len(kl) >= 4 and re.search(rf"(?<!\w){escaped}(?=\w)", norm, flags=re.UNICODE):
            seen_lower.add(kl)
            found.append(original)
            continue

        # Short English keys often appear with punctuation stuck: "(ban)", "ban.", "ban!"
        if re.search(rf"(?<!\w){escaped}(?=\W|$)", norm, flags=re.UNICODE):
            seen_lower.add(kl)
            found.append(original)
            continue

    return found


def _sentiment(text: str) -> Tuple[str, float]:
    if not VADER_AVAILABLE:
        return "N/A", 0.0
    scores = _sia.polarity_scores(text)
    compound = scores["compound"]
    if compound >= 0.05:
        label = "positive"
    elif compound <= -0.05:
        label = "negative"
    else:
        label = "neutral"
    return label, round(compound, 4)


def _touch_progress(job_state: Optional[Dict[str, Any]], **kwargs: Any) -> None:
    if job_state and "progress" in job_state:
        job_state["progress"].update(kwargs)


def _cancelled(job_state: Optional[Dict[str, Any]]) -> bool:
    if not job_state:
        return False
    ev = job_state.get("cancel")
    return bool(ev and ev.is_set())


async def _run_with_fetch(
    app_id: int,
    game_name: str,
    keywords: List[str],
    max_pages: int,
    fetch_fn: FetchFn,
    errors: List[str],
    job_state: Optional[Dict[str, Any]] = None,
) -> ScrapeResult:
    keywords = [str(k).strip() for k in keywords if k is not None and str(k).strip()]
    discussion_url = DISCUSSION_BASE.format(app_id=app_id)
    all_threads: List[Tuple[str, str]] = []
    flagged: List[FlaggedComment] = []
    total_comments = 0
    pages_scraped = 0

    _touch_progress(
        job_state,
        phase="listing_pages",
        message="Fetching discussion list pages…",
        max_pages=max_pages,
        page_index=0,
        threads_discovered=0,
        thread_index=0,
        threads_total=0,
        current_thread="",
        comments_scanned=0,
        flagged_count=0,
    )

    for page in range(max_pages):
        if _cancelled(job_state):
            errors.append("Stopped by user during listing.")
            break
        url = THREAD_LIST_URL.format(app_id=app_id, page=page)
        try:
            html = await fetch_fn(url, ".forum_topic_name", 2.5, is_thread_page=False)
            threads_on_page = _parse_thread_links(html, app_id)
            if not threads_on_page:
                logger.info("No threads on page %s, stopping pagination.", page)
                break
            all_threads.extend(threads_on_page)
            pages_scraped += 1
            _touch_progress(
                job_state,
                page_index=page + 1,
                pages_scraped=pages_scraped,
                threads_discovered=len(all_threads),
                message=f"List page {page + 1}/{max_pages}: +{len(threads_on_page)} threads",
            )
            logger.info("Page %s: found %s threads.", page, len(threads_on_page))
        except Exception as exc:
            errors.append(f"Error fetching thread list page {page}: {exc}")
            break

    seen = set()
    unique_threads: List[Tuple[str, str]] = []
    for t in all_threads:
        if t[1] not in seen:
            seen.add(t[1])
            unique_threads.append(t)

    n_threads = len(unique_threads)
    logger.info("Total unique threads to scrape: %s", n_threads)
    _touch_progress(
        job_state,
        phase="scraping_threads",
        threads_total=n_threads,
        thread_index=0,
        message=f"Scanning {n_threads} threads for keywords…",
    )

    for ti, (thread_title, thread_url) in enumerate(unique_threads):
        if _cancelled(job_state):
            errors.append("Stopped by user during thread scan.")
            break
        _touch_progress(
            job_state,
            thread_index=ti + 1,
            current_thread=thread_title[:80] + ("…" if len(thread_title) > 80 else ""),
            message=f"Thread {ti + 1}/{n_threads}: loading…",
        )
        try:
            html = await fetch_fn(
                thread_url,
                ".forum_comment, .commentthread_comment, .forum_op, .forum_topic_op",
                2.5,
                is_thread_page=True,
            )
            comments = _parse_comments(html, thread_title, thread_url)
            if not comments:
                _touch_progress(
                    job_state,
                    message=(
                        f"Thread {ti + 1}/{n_threads}: no comments parsed "
                        "(layout may differ or page still loading)"
                    ),
                )
            for idx, c in enumerate(comments):
                if _cancelled(job_state):
                    errors.append("Stopped by user while reading comments.")
                    break
                total_comments += 1
                matched = _find_keywords(c["text"], keywords)
                if matched:
                    sentiment_label, sentiment_score = _sentiment(c["text"])
                    flagged.append(
                        FlaggedComment(
                            thread_title=c["thread_title"],
                            thread_url=c["thread_url"],
                            author=c["author"],
                            timestamp=c["timestamp"],
                            comment_text=c["text"],
                            matched_keywords=matched,
                            sentiment=sentiment_label,
                            sentiment_score=sentiment_score,
                        )
                    )
                if job_state and (idx % 3 == 0 or idx == len(comments) - 1):
                    _touch_progress(
                        job_state,
                        comments_scanned=total_comments,
                        flagged_count=len(flagged),
                        message=(
                            f"Thread {ti + 1}/{n_threads}: read {idx + 1}/{len(comments)} comments "
                            f"({total_comments} total, {len(flagged)} flagged)"
                        ),
                    )
            if _cancelled(job_state):
                break
        except Exception as exc:
            errors.append(f"Error scraping thread '{thread_title}': {exc}")

    kw_counter: Counter = Counter()
    for fc in flagged:
        kw_counter.update(fc.matched_keywords)

    _touch_progress(
        job_state,
        phase="done" if not _cancelled(job_state) else "stopped",
        message="Finished" if not _cancelled(job_state) else "Stopped",
    )

    return ScrapeResult(
        app_id=app_id,
        game_name=game_name,
        discussion_url=discussion_url,
        pages_scraped=pages_scraped,
        threads_found=len(unique_threads),
        comments_scanned=total_comments,
        flagged_comments=flagged,
        keyword_frequency=dict(kw_counter),
        errors=errors,
    )


async def run_scrape_pipeline(
    app_id: int,
    game_name: str,
    keywords: List[str],
    max_pages: int,
    job_state: Optional[Dict[str, Any]] = None,
) -> ScrapeResult:
    """
    Always uses Playwright when installed; otherwise httpx only.
    On Playwright launch/runtime errors, falls back to httpx and records errors.
    Optional job_state: { "progress": dict, "cancel": asyncio.Event } for UI progress / stop.
    """
    errors: List[str] = []

    async def fetch_http(
        url: str,
        _wait: Optional[str] = None,
        _pause: float = 0,
        _thread: bool = False,
    ) -> str:
        return await _fetch_html_http(url)

    if not PLAYWRIGHT_AVAILABLE:
        errors.append("Playwright is not installed; using HTTP-only fetching.")
        return await _run_with_fetch(
            app_id, game_name, keywords, max_pages, fetch_http, errors, job_state
        )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                page = await context.new_page()

                async def fetch_pw(
                    url: str,
                    wait_selector: Optional[str] = None,
                    pause: float = 2.0,
                    is_thread_page: bool = False,
                ) -> str:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    if is_thread_page:
                        selectors = [
                            ".forum_comment",
                            ".commentthread_comment",
                            ".forum_op",
                            ".forum_topic_op",
                        ]
                    elif wait_selector:
                        selectors = [s.strip() for s in wait_selector.split(",") if s.strip()]
                    else:
                        selectors = []

                    for sel in selectors:
                        try:
                            await page.wait_for_selector(sel, timeout=12_000)
                            break
                        except PlaywrightTimeout:
                            continue

                    await asyncio.sleep(pause)
                    return await page.content()

                return await _run_with_fetch(
                    app_id, game_name, keywords, max_pages, fetch_pw, errors, job_state
                )
            finally:
                await browser.close()
    except Exception as exc:
        detail = _format_playwright_error(exc)
        logger.exception("Playwright scrape path failed: %s", detail)
        errors.append(
            f"Playwright failed ({detail}). Falling back to HTTP. {_PLAYWRIGHT_HINT}"
        )
        return await _run_with_fetch(
            app_id,
            game_name,
            keywords,
            max_pages,
            fetch_http,
            list(errors),
            job_state,
        )


async def _fetch_html_http(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text
