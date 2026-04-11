# Steam Discussion Scraper — production image (FastAPI + Playwright Chromium + PostgreSQL)
# On Render: set DATABASE_URL to your Postgres **Internal Database URL** (Environment tab),
# or use render.yaml fromDatabase. The app does not use localhost on Render.

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    LOW_MEMORY=1 \
    DB_POOL_MAX=2

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && python -c "import nltk; nltk.download('vader_lexicon', quiet=True)"

COPY app ./app
COPY index.html ./index.html
COPY run.py ./run.py

RUN mkdir -p static

EXPOSE 8000

# Render sets PORT; bind publicly for the edge proxy
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
