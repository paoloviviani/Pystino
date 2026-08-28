# The chat service and, optionally, the SPA it serves.
#
# Same two-image arrangement as the gateway (ADR 0023) and for the same reason:
# `chat-api` is the API alone, `chat` also carries the built web app. The HTTP
# API is identical in both — the SPA is additive, never a behaviour change.
ARG INCLUDE_WEB=false

FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Manifests first so the dependency layer caches independently of source.
# Every workspace member's manifest is needed because uv resolves the whole
# workspace from one lockfile, even when only one member is being built.
COPY pyproject.toml uv.lock ./
COPY apps/chat-api/pyproject.toml apps/chat-api/
COPY apps/gateway/pyproject.toml apps/gateway/
COPY packages/shared-py/pyproject.toml packages/shared-py/
COPY apps/chat-api/README.md apps/chat-api/
COPY apps/gateway/README.md apps/gateway/
COPY packages/shared-py/README.md packages/shared-py/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package chat-api

COPY apps/chat-api/src apps/chat-api/src
COPY apps/chat-api/migrations apps/chat-api/migrations
COPY apps/chat-api/alembic.ini apps/chat-api/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package chat-api


# ---------------------------------------------------------------------------
# The SPA. Built unconditionally only when something copies from this stage —
# BuildKit skips stages nothing references, so the API-only image pays nothing.
FROM node:24.19.0-slim AS web-build

WORKDIR /build
RUN corepack enable

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/ui/package.json packages/ui/
COPY apps/web/package.json apps/web/
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter @llmp/web...

COPY packages/ui packages/ui
COPY apps/web apps/web
RUN pnpm --filter @llmp/web build && cp -r apps/web/dist /out


# Both branches must offer the path the COPY names: `COPY --from` fails on a
# missing source and Docker cannot make a COPY conditional. busybox rather than
# scratch, because scratch has no shell to create the directory with.
FROM busybox:1.37-uclibc AS web-false
RUN mkdir -p /out

FROM web-build AS web-true

# `--from` cannot interpolate a variable but `FROM` can, which is the standard
# way round it.
ARG INCLUDE_WEB
FROM web-${INCLUDE_WEB} AS web-selected


FROM python:3.13-slim AS runtime

RUN groupadd --system --gid 1001 chat \
    && useradd --system --uid 1001 --gid chat --no-create-home chat

WORKDIR /app

COPY --from=builder --chown=chat:chat /app /app

# Landing on the path routers/spa.py looks at, so neither image needs
# configuration. An empty directory is a complete API-only image: the service
# checks for index.html at startup and serves the API alone if it is absent.
COPY --from=web-selected --chown=chat:chat \
     /out/ /app/apps/chat-api/src/chat_api/web_static/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER chat
EXPOSE 8100

CMD ["chat-api"]
