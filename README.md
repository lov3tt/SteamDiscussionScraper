# 🎮 Steam NLP Discussion Board Scraper

A full-stack tool to search Steam games, scrape their community discussion boards, and detect flagged keywords (cheating, scam, ban, etc.) using NLP + sentiment analysis.

---

## Architecture

```
steam_nlp_scraper/
├── app/
│   ├── main.py                   # FastAPI app entrypoint + CORS + static serving
│   ├── models/
│   │   └── schemas.py            # Pydantic request/response models
│   ├── routers/
│   │   ├── search.py             # GET /api/search/  — Steam Store search
│   │   └── scrape.py             # POST /api/scrape/ — Discussion board ETL
│   └── services/
│       ├── steam_api.py          # Steam Store API (search + app details)
│       └── scraper.py            # ETL pipeline: Selenium → BS4 → NLP
├── frontend/
│   └── index.html                # Dark-themed SPA dashboard (no build step)
├── run.py                        # Uvicorn entrypoint
└── requirements.txt
```

---

## ETL Pipeline

```
[ Steam Store API ]
       │ game search / appID lookup
       ▼
[ Selenium (headless Chrome) ]
       │ loads JS-rendered discussion list pages
       │ page-by-page pagination up to max_pages
       ▼
[ BeautifulSoup4 ]
       │ parses HTML → thread URLs
       │ parses each thread → comment blocks
       ▼
[ NLP Layer ]
       │ Regex word-boundary keyword matching
       │ VADER sentiment analysis (NLTK)
       ▼
[ FastAPI JSON response ]
       │ flagged comments, keyword frequency, sentiment labels
       ▼
[ Frontend Dashboard ]
         live stats, frequency bars, filterable comment cards
```

---

## Installation

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Install ChromeDriver

Selenium requires ChromeDriver that matches your installed Chrome version.

**macOS (Homebrew):**
```bash
brew install chromedriver
```

**Linux:**
```bash
sudo apt install chromium-driver
# or download from https://chromedriver.chromium.org/downloads
```

**Windows:**
Download from https://chromedriver.chromium.org/downloads and add to PATH.

### 3. Download NLTK VADER lexicon (auto-download on first run, or manual)

```python
import nltk; nltk.download('vader_lexicon')
```

---

## Running the App

```bash
python run.py
# or
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open: **http://localhost:8000**

The FastAPI Swagger docs are at: **http://localhost:8000/docs**

---

## API Endpoints

### Search

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/search/?q={query}&max_results=10` | Search Steam games by name |
| `GET` | `/api/search/details/{app_id}` | Full app metadata |

### Scrape

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/scrape/` | Synchronous scrape (blocks until done) |
| `POST` | `/api/scrape/start` | Background job, returns `job_id` |
| `GET` | `/api/scrape/status/{job_id}` | Poll job status + result |

### Scrape Request Body

```json
{
  "app_id": 730,
  "game_name": "Counter-Strike 2",
  "keywords": ["cheating", "ban", "unfair", "hack", "scam"],
  "max_pages": 5,
  "use_selenium": true
}
```

### Scrape Response

```json
{
  "app_id": 730,
  "game_name": "Counter-Strike 2",
  "discussion_url": "https://steamcommunity.com/app/730/discussions/0/",
  "pages_scraped": 5,
  "threads_found": 47,
  "comments_scanned": 312,
  "flagged_comments": [
    {
      "thread_title": "Cheaters are ruining ranked",
      "thread_url": "https://steamcommunity.com/app/730/discussions/0/...",
      "author": "username",
      "timestamp": "12 Apr @ 3:22pm",
      "comment_text": "I got banned for no reason, this is unfair...",
      "matched_keywords": ["banned", "unfair"],
      "sentiment": "negative",
      "sentiment_score": -0.6249
    }
  ],
  "keyword_frequency": {
    "cheating": 14,
    "ban": 9,
    "unfair": 7
  },
  "errors": []
}
```

---

## Frontend Features

- 🔍 **Game Search** — type any game name, get cards with images, prices, app IDs
- ✅ **Select Game** — click a card to target it for scraping
- 🏷️ **Keyword Manager** — add/remove keywords before scraping
- ⚙️ **Config** — set max pages, toggle Selenium on/off
- 📊 **Live Progress** — log stream + animated progress bar
- 📈 **Keyword Frequency Chart** — visual bar chart of matched keyword counts
- 💬 **Flagged Comment Cards** — highlighted keywords, sentiment badges, thread links
- 🔽 **Filters** — filter by sentiment (positive/negative/neutral) or keyword

---

## Notes

- **Steam rate limits**: Steam may throttle requests. Add delays between pages in `scraper.py` (`pause=` param) if you hit 429s.
- **Selenium fallback**: If `use_selenium=false`, the scraper falls back to `httpx` (requests), which may miss JS-rendered content.
- **Production**: Swap the in-memory `_jobs` dict in `scrape.py` for Redis + Celery for concurrent job management.
- **Proxy support**: For large-scale scraping, configure an HTTP proxy in `_build_driver()` and `_fetch_html_requests()`.
