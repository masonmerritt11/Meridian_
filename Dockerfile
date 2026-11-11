# Single image: Next.js command center (static export) + FastAPI API + in-process workers.
# ---- frontend build
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- runtime
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 STATIC_DIR=/app/static
WORKDIR /app
RUN useradd --create-home --uid 10001 meridian
COPY backend/pyproject.toml ./backend/
COPY backend/meridian ./backend/meridian
RUN pip install ./backend
COPY backend/alembic.ini ./backend/
COPY backend/migrations ./backend/migrations
COPY --from=web /web/out ./static
WORKDIR /app/backend
USER meridian
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health',timeout=4)"
CMD ["python", "-m", "meridian.cli", "serve"]
