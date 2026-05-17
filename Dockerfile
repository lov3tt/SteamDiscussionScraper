# Render.com: use "Docker" runtime (not native Python) so Playwright + Chromium work.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && python -c "import nltk; nltk.download('vader_lexicon')" \
    && python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage']); b.close(); p.stop(); print('chromium ok')"

COPY . .

EXPOSE 10000

# Re-install browsers if Render's layer cache skipped the build RUN step
CMD ["sh", "-c", "python -m playwright install chromium 2>/dev/null || true; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
