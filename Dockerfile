# SportBroBot container image (used by Railway and any Docker host).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SPORTBRO_DATA_DIR=/data

WORKDIR /app

# Install dependencies first so this layer caches across code changes.
COPY pyproject.toml ./
COPY sportbrobot ./sportbrobot
RUN pip install --no-cache-dir .

# SQLite DB + generated keys live here. Mount a Railway volume at /data (or a
# Docker volume) to keep users, links and encryption keys across redeploys.
RUN mkdir -p /data

EXPOSE 8000

# Railway injects $PORT; fall back to 8000 for plain `docker run`.
CMD ["sh", "-c", "uvicorn sportbrobot.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
