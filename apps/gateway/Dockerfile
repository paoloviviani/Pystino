# Multi-stage: dependencies are resolved and installed in a builder layer, then
# only the resulting virtualenv and source are copied into the runtime image.
# Declared before the first FROM so it is in global scope, which is what lets
# the console selector stage below interpolate it. See ADR 0023.
ARG INCLUDE_CONSOLE=false

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
# Every workspace member's manifest, even ones this image does not contain: uv
# resolves the whole workspace from one lockfile and fails on a member it cannot
# read. Only the manifest is needed, never the source — `--package gateway`
# below is what keeps any other member out of this image.
# Hatchling validates that the readme referenced by each manifest exists.
COPY apps/gateway/README.md apps/gateway/
COPY packages/shared-py/README.md packages/shared-py/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package gateway

COPY apps/gateway/src apps/gateway/src
COPY packages/shared-py/src packages/shared-py/src
COPY apps/gateway/migrations apps/gateway/migrations
COPY apps/gateway/alembic.ini apps/gateway/

# `--package gateway`, not a bare sync: a bare sync installs every workspace
# member, so one member's presence or absence would couple this build to a
# change in a different service — the failure mode that made this explicit.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package gateway


# ---------------------------------------------------------------------------
# Console (ADR 0023). Two images from one Dockerfile: `gateway` is headless,
# `gateway-console` also serves the SPA. The HTTP API is identical in both — the
# console is additive, never a behaviour change.
#
#   docker build --build-arg INCLUDE_CONSOLE=true .
#
# The Node stage runs unconditionally because BuildKit skips stages nothing
# copies from: with INCLUDE_CONSOLE=false the `console` stage below is never
# referenced, so it is never built, and the headless image pays nothing for it.
FROM node:24.19.0-slim AS console-build

WORKDIR /build
RUN corepack enable

# Manifests first so the dependency layer caches independently of source.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/ui/package.json packages/ui/
COPY apps/console/package.json apps/console/
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile --filter @llmp/console...

COPY packages/ui packages/ui
COPY apps/console apps/console
RUN pnpm --filter @llmp/console build \
    && cp -r apps/console/dist /out


# The headless alternative: the same /out path, empty. Both branches must offer
# the path the COPY names, because `COPY --from` fails on a missing source and
# Docker cannot make a COPY conditional. `scratch` will not do — there is no
# shell in it to create the directory with.
FROM busybox:1.37-uclibc AS console-false
RUN mkdir -p /out

FROM console-build AS console-true

# `--from` cannot interpolate a variable, but `FROM` can. Resolving the choice
# into a fixed stage name here is the standard way round that.
ARG INCLUDE_CONSOLE
FROM console-${INCLUDE_CONSOLE} AS console-selected


FROM python:3.13-slim AS runtime

# Non-root. The gateway needs no write access to its own filesystem.
RUN groupadd --system --gid 1001 gateway \
    && useradd --system --uid 1001 --gid gateway --no-create-home gateway

WORKDIR /app

COPY --from=builder --chown=gateway:gateway /app /app

# `console-true` is the built SPA, `console-false` an empty directory, chosen by
# the build arg. Landing straight on the path routers/console.py looks at, so no
# configuration is needed in either image. The gateway checks for index.html at
# startup and serves the API only if it is absent — an empty directory is a
# complete, working headless image, not a broken console.
COPY --from=console-selected --chown=gateway:gateway \
     /out/ /app/apps/gateway/src/gateway/console_static/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER gateway
EXPOSE 8000

# The gateway is stateless, so scale with worker processes. Under the GIL,
# threads would not help this workload.
CMD ["gateway", "serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
