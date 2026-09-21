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

# tippecanoe: previously a dev-only tool (manual `tippecanoe -o ...` after
# running a fetch/generate script), now a runtime dependency — the
# per-dataset refresh API tiles polygon datasets itself.
#
# Built from source here rather than copying a prebuilt binary from e.g. the
# klokantech/tippecanoe image (what this project used for every manual
# tiling run so far): that image is amd64-only, and a straight binary copy
# into an arm64 runtime (this project builds arm64-native on Apple Silicon —
# confirmed via `docker image inspect`) would be a non-executable binary,
# not just a library mismatch. Building from source compiles natively for
# whatever architecture is actually running the build, on any host.
FROM python:3.12-slim AS tippecanoe-build
RUN apt-get update && apt-get install -y --no-install-recommends \
      git build-essential libsqlite3-dev zlib1g-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/felt/tippecanoe.git /tippecanoe
WORKDIR /tippecanoe
RUN make -j"$(nproc)" && make install PREFIX=/usr/local

FROM runtime AS runtime-with-tippecanoe
COPY --from=tippecanoe-build /usr/local/bin/tippecanoe /usr/local/bin/tippecanoe-decode /usr/local/bin/tile-join /usr/local/bin/

# Backend code + bundled sample parquet (so the image runs out of the box).
COPY backend/ ./
# Compiled frontend from stage 1, served by FastAPI at "/".
COPY --from=frontend /app/frontend/dist ./static

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
