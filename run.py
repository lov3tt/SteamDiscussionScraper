#!/usr/bin/env python3
"""
Entry point for the Steam NLP Scraper API.
Run: python run.py

Reload is off by default: on Windows, uvicorn's reloader spawns a child process where
Playwright often fails to launch Chromium (and may surface as an empty exception).

Dev reload: set UVICORN_RELOAD=1 or run:
  uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
"""

import os
import uvicorn

if __name__ == "__main__":
    # Define the 'port' variable here so the code below can see it
    port = int(os.environ.get("PORT", 8000)) 
    
    _reload = os.environ.get("UVICORN_RELOAD", "").strip().lower() in ("1", "true", "yes")
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,  # Now 'port' is defined!
        reload=_reload,
        log_level="info",
    )

