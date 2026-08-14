# Multi-stage: dependencies are resolved and installed in a builder layer, then
# only the resulting virtualenv and source are copied into the runtime image.
FROM python:3.13-slim AS builder

# Pinned rather than :latest — a build tool that changes under you produces
# unreproducible images.
COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Copy only the manifests first, so the dependency layer is cached and does not
# rebuild every time application code changes.
COPY pyproject.toml uv.lock ./
COPY apps/gateway/pyproject.toml apps/gateway/
COPY packages/shared-py/pyproject.toml packages/shared-py/
# Hatchling validates that the readme referenced by each manifest exists.
COPY apps/gateway/README.md apps/gateway/
COPY packages/shared-py/README.md packages/shared-py/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace

COPY apps/gateway/src apps/gateway/src
COPY packages/shared-py/src packages/shared-py/src
COPY apps/gateway/migrations apps/gateway/migrations
COPY apps/gateway/alembic.ini apps/gateway/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim AS runtime

# Non-root. The gateway needs no write access to its own filesystem.
RUN groupadd --system --gid 1001 gateway \
    && useradd --system --uid 1001 --gid gateway --no-create-home gateway

WORKDIR /app

COPY --from=builder --chown=gateway:gateway /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER gateway
EXPOSE 8000

# The gateway is stateless, so scale with worker processes. Under the GIL,
# threads would not help this workload.
CMD ["gateway", "serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
