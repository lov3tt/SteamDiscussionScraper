"""
Steam Discussion Board Scraper
──────────────────────────────
ETL Pipeline:
  Extract  → Selenium loads each JS-rendered discussion-board page
  Transform → BeautifulSoup4 parses the HTML into structured thread/comment data
  Load      → NLP keyword matching + VADER sentiment analysis on each comment

Discussion board URL pattern:
  https://steamcommunity.com/app/{app_id}/discussions/

Thread list page pattern:
  https://steamcommunity.com/app/{app_id}/discussions/0/?fp={page}

Each thread page:
  https://steamcommunity.com/app/{app_id}/discussions/0/{thread_id}/
"""

import re
import time
import logging
from typing import List, Dict, Tuple, Optional
from collections import Counter

from bs4 import BeautifulSoup

# ── Selenium (optional graceful fallback) ───────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# ── Requests fallback ───────────────────────────────────────────────────────
import httpx

# ── NLP: VADER sentiment (ships with NLTK, no model download needed) ────────
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

DISCUSSION_BASE = "https://steamcommunity.com/app/{app_id}/discussions/0/"
THREAD_LIST_URL = "https://steamcommunity.com/app/{app_id}/discussions/0/?fp={page}"

# ── Selenium driver factory ──────────────────────────────────────────────────

def _build_driver() -> "webdriver.Chrome":
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(options=opts)


# ── Page fetchers ─────────────────────────────────────────────────────────────

def _fetch_html_selenium(driver, url: str, wait_selector: str = None, pause: float = 2.0) -> str:
    driver.get(url)
    if wait_selector:
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
            )
        except TimeoutException:
            pass
    time.sleep(pause)          # let lazy-loaded content settle
    return driver.page_source


async def _fetch_html_requests(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


# ── ETL — Extract ─────────────────────────────────────────────────────────────

def _parse_thread_links(html: str, app_id: int) -> List[Tuple[str, str]]:
    """Return list of (thread_title, thread_url) from a discussion list page."""
    soup = BeautifulSoup(html, "html.parser")
    threads = []

    # Steam discussion list: each thread is an <a> with class 'forum_topic_name'
    for a in soup.select("a.forum_topic_name"):
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if href and str(app_id) in href:
            threads.append((title, href))

    # Fallback: any link that looks like a discussion thread URL
    if not threads:
        pattern = re.compile(rf"steamcommunity\.com/app/{app_id}/discussions/\d+/\d+")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if pattern.search(href):
                title = a.get_text(strip=True) or href
                threads.append((title, href))

    return threads


def _parse_comments(html: str, thread_title: str, thread_url: str) -> List[Dict]:
    """Parse all comments from a single thread page."""
    soup = BeautifulSoup(html, "html.parser")
    comments = []

    # Steam thread comments live inside '.forum_comment_permlink' containers
    comment_blocks = soup.select(".forum_comment_permlink")

    # Fallback selectors used by Steam's responsive layout
    if not comment_blocks:
        comment_blocks = soup.select(".commentthread_comment")

    for block in comment_blocks:
        # Author
        author_el = (
            block.select_one(".forum_comment_author a")
            or block.select_one(".commentthread_comment_author a")
            or block.select_one(".bbs_author a")
        )
        author = author_el.get_text(strip=True) if author_el else "Unknown"

        # Timestamp
        ts_el = (
            block.select_one(".forum_comment_date")
            or block.select_one(".commentthread_comment_timestamp")
            or block.select_one(".date")
        )
        timestamp = ts_el.get_text(strip=True) if ts_el else None

        # Comment body
        body_el = (
            block.select_one(".forum_comment_text")
            or block.select_one(".commentthread_comment_text")
            or block.select_one(".bbs_content")
        )
        text = body_el.get_text(separator=" ", strip=True) if body_el else ""

        if text:
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


# ── ETL — Transform (NLP) ─────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return text.lower()


def _find_keywords(text: str, keywords: List[str]) -> List[str]:
    """Return list of keywords found in text (word-boundary aware)."""
    norm = _normalize(text)
    found = []
    for kw in keywords:
        pattern = re.compile(rf"\b{re.escape(kw.lower())}\b")
        if pattern.search(norm):
            found.append(kw)
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


# ── ETL — Main pipeline ───────────────────────────────────────────────────────

async def run_scrape_pipeline(
    app_id: int,
    game_name: str,
    keywords: List[str],
    max_pages: int,
    use_selenium: bool,
) -> ScrapeResult:
    """
    Full ETL pipeline:
      1. For each discussion-list page (up to max_pages):
         a. Load HTML (Selenium or requests)
         b. Parse thread links
      2. For each thread:
         a. Load HTML
         b. Parse comments
         c. Match keywords + run sentiment
      3. Aggregate results → ScrapeResult
    """
    discussion_url = DISCUSSION_BASE.format(app_id=app_id)
    errors: List[str] = []
    all_threads: List[Tuple[str, str]] = []
    flagged: List[FlaggedComment] = []
    total_comments = 0
    pages_scraped = 0

    driver = None
    if use_selenium and SELENIUM_AVAILABLE:
        try:
            driver = _build_driver()
            logger.info("Selenium driver started.")
        except Exception as exc:
            errors.append(f"Selenium driver failed to start: {exc}. Falling back to requests.")
            driver = None

    # ── Step 1: Collect thread URLs ──────────────────────────────────────────
    for page in range(max_pages):
        url = THREAD_LIST_URL.format(app_id=app_id, page=page)
        try:
            if driver:
                html = _fetch_html_selenium(
                    driver, url,
                    wait_selector=".forum_topic_name",
                    pause=2.5
                )
            else:
                html = await _fetch_html_requests(url)

            threads_on_page = _parse_thread_links(html, app_id)
            if not threads_on_page:
                logger.info(f"No threads on page {page}, stopping pagination.")
                break

            all_threads.extend(threads_on_page)
            pages_scraped += 1
            logger.info(f"Page {page}: found {len(threads_on_page)} threads.")
        except Exception as exc:
            errors.append(f"Error fetching thread list page {page}: {exc}")
            break

    # Deduplicate by URL
    seen = set()
    unique_threads = []
    for t in all_threads:
        if t[1] not in seen:
            seen.add(t[1])
            unique_threads.append(t)

    logger.info(f"Total unique threads to scrape: {len(unique_threads)}")

    # ── Step 2: Scrape each thread ──────────────────────────────────────────
    for thread_title, thread_url in unique_threads:
        try:
            if driver:
                html = _fetch_html_selenium(
                    driver, thread_url,
                    wait_selector=".forum_comment_permlink",
                    pause=2.0
                )
            else:
                html = await _fetch_html_requests(thread_url)

            comments = _parse_comments(html, thread_title, thread_url)
            total_comments += len(comments)

            for c in comments:
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
        except Exception as exc:
            errors.append(f"Error scraping thread '{thread_title}': {exc}")

    if driver:
        try:
            driver.quit()
        except Exception:
            pass

    # ── Step 3: Aggregate keyword frequency ──────────────────────────────────
    kw_counter: Counter = Counter()
    for fc in flagged:
        kw_counter.update(fc.matched_keywords)

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
