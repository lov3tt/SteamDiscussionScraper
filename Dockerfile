# Steam Discussion Scraper — production image (FastAPI + Playwright Chromium + PostgreSQL)
# Deploy on Render.com as a Web Service (Docker). Link a Render PostgreSQL instance; set
# DATABASE_URL via Blueprint or the dashboard (Render injects it automatically).

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

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
