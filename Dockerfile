# syntax=docker/dockerfile:1
#
# The whole simulation platform in one image: the API on port 8000 serves the built web UI.
#
#   docker compose up --build             # the app, with a volume for its blobs
#   docker build -t freight-agent .       # the image alone
#
# No key and no .env go into the image (.dockerignore keeps them out of the context). Every
# setting comes from the environment at run time, and the default LLM_MODE=replay (recorded
# answers in evals/cassettes) needs no key and no network.

# --- stage 1: the web UI ------------------------------------------------------------------
FROM node:22-alpine AS web

# The pnpm the lockfile was written with (lockfile v9).
RUN npm install --global pnpm@10.33.0

WORKDIR /web
# The manifests first: a source change does not reinstall the packages.
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
# Type-checks, then writes dist/: index.html and the hashed files under assets/.
RUN pnpm build

# --- stage 2: the API and the agent -------------------------------------------------------
FROM python:3.13-slim AS app

# uv pinned to the version uv.lock is made with.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The dependencies first, in their own layer. README.md is the package's readme, which the
# build backend reads.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the project itself, as an editable install.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# What the app reads at run time, at the Settings defaults (relative to /app): the SOP
# tree, the scenarios, their fixtures (load metadata, the Acme SOP, the POD and BOL PDFs)
# and cassettes, the model sets and the built UI.
COPY sops ./sops
COPY evals ./evals
COPY config ./config
COPY --from=web /web/dist ./web/dist

# A non-root user. The code and the venv stay owned by root, so the app cannot change
# them; it may only write eval results (`agent eval run`) and blobs: the uploaded
# attachments and SOP files (BLOBS_DIR, a volume in docker-compose.yml).
RUN useradd --system --user-group --no-log-init --create-home --uid 10001 app \
    && mkdir -p evals/results blobs \
    && chown app:app evals/results blobs
USER app

ENV PATH="/app/.venv/bin:$PATH" \
    LLM_MODE=replay \
    WEB_DIST=/app/web/dist \
    BLOBS_DIR=/app/blobs

EXPOSE 8000

# Liveness: /api/health answers 200 while the app serves (its status field is about the
# simulation, not the process). No curl in the slim image, so the venv's Python asks.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]

# The `agent` CLI of the venv, so `docker compose run --rm app eval run --mode replay`
# works too.
ENTRYPOINT ["agent"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
