# syntax=docker/dockerfile:1

# ---- Stage 1: build the React frontend --------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# The app is served by FastAPI on the same origin, so use relative /api paths.
ENV VITE_API_BASE=""
RUN npm run build

# ---- Stage 2: Python runtime serving the API + the built UI -----------------
FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    STATIC_DIR=/app/static \
    DATA_DIR=/app/data \
    PIP_DEFAULT_TIMEOUT=120

COPY backend/requirements.txt ./
# BuildKit cache mount: pip's downloads persist across builds, so a timeout or a
# re-run resumes from what's already been fetched instead of starting over.
# Longer timeout + retries ride out slow/stalling connections.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 10 -r requirements.txt

# Backend code + bundled sample parquet (so the image runs out of the box).
COPY backend/ ./
# Compiled frontend from stage 1, served by FastAPI at "/".
COPY --from=frontend /app/frontend/dist ./static

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
